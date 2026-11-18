"""The API's routes (docs/architecture.md, 7): on loopback for a local rrotor's caller, at `<machine>.<domain>` for a
hosted one's callers, at its one public host for a self-hosted one's. The operations themselves are in ops.py, shared
with the MCP server (mcp.py).

What changes the runtime itself, not its programs (its link's token, its parts, its licence), answers only on a local
rrotor, to its owner's mod with the local token. A hosted or self-hosted rrotor takes those from its environment, its
image and Glyphh (the link, the heartbeat, a licence key in GLYPHH_LICENSE_KEY or /etc/rrotor/license.key), never
from a caller: a runtime key or a Glyphh sign-in calls and deploys programs, and is refused E_LOCAL_ONLY there."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from aiohttp import web

from rrotord import VERSION, apps
from rrotord import drain as drains
from rrotord.access import Access
from rrotord.aggregate import ClusterAudit
from rrotord.api import mcp, ops
from rrotord.api.gate import METADATA, SCOPE, public_url, viewer_roles
from rrotord.audit import Audit, current
from rrotord.browser import Browser
from rrotord.catalogue import Catalogue
from rrotord.cluster.node import Cluster
from rrotord.config import Config
from rrotord.deploy import Deployer
from rrotord.drain import Drain
from rrotord.errors import BadRequest, Forbidden, NotFound, RrotorError, Unavailable
from rrotord.introspect import Introspector
from rrotord.licence import Licence
from rrotord.link.credentials import Credentials
from rrotord.parts import Parts
from rrotord.programs import Programs, Version
from rrotord.runner.engine import Runner
from rrotord.sandbox.probe import Layers
from rrotord.scheduler import Scheduler

BODY_MAX = 96 * 1024 * 1024  # a bundle in base64, with room


Handler = Callable[[web.Request], Awaitable[web.Response]]


class Fetch(Protocol):
    async def __call__(self, url: str) -> bytes: ...


class Report(Protocol):
    """`deployed` up the link: the version, its repo (if any) and, for an app published, who published it."""

    async def __call__(self, version: Version, repo: str | None, by: str | None = None) -> None: ...


class LinkView(Protocol):
    def view(self) -> dict[str, Any]: ...


class HeartbeatView(Protocol):
    def view(self) -> dict[str, Any] | None: ...


@dataclass(slots=True)
class Services:
    config: Config
    programs: Programs
    deployer: Deployer
    runner: Runner
    credentials: Credentials
    link: LinkView
    layers: Layers
    parts: Parts
    licence: Licence
    fetch: Fetch | None = None  # downloads a part by URL; none in tests
    access: Access | None = None  # hosted: the runtime keys and the pause
    introspect: Introspector | None = None  # hosted: a Glyphh sign-in checked with the Cloud
    report: Report | None = None  # after every deploy, `deployed` up the link; none in tests without a link
    heartbeat: HeartbeatView | None = None  # self, the link on: the last heartbeat
    audit: Audit | None = None  # the audit events: kept (Enterprise) and read by GET /v1/audit, or logged
    drain: Drain | None = None  # the customer's collector and server-side logging, when either is on
    cluster: Cluster | None = None  # this node's place in its cluster; none: standalone
    gathered: ClusterAudit | None = None  # the cluster's audit store, read on its head
    scheduler: Scheduler | None = None  # the programs' schedules: fired by the daemon, listed and paused here
    catalogue: Catalogue | None = None  # the catalogue's word on the apps here (docs/architecture.md, 5.8)
    browser: Browser | None = None  # public, the link on: a browser's Glyphh sign-in, to open the apps


async def body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise BadRequest("The body must be JSON.") from None
    if not isinstance(data, dict):
        raise BadRequest("The body must be a JSON object.")
    return data


def local_only(mode: str, handler: Handler) -> Handler:
    """The handler on a local rrotor; anywhere else, a refusal: the runtime is not changed by its callers."""

    async def refused(_: web.Request) -> web.Response:
        raise Forbidden(
            f"A {mode} runtime takes its link token, parts and licence from its environment, its image and Glyphh, "
            "not over the API.",
            code="E_LOCAL_ONLY",
        )

    return handler if mode == "local" else refused


async def audited(s: Services, query: dict[str, Any]) -> dict[str, Any]:
    """`GET /v1/audit` and `audit_query`: the kept audit events, Enterprise only; in a cluster, the cluster's, which
    its head keeps (a follower asks it)."""
    if s.audit is None or not s.audit.kept:
        raise RrotorError(
            "The audit log is part of the Enterprise plan; below it audit events are log lines (logger rrotor.audit).",
            code="E_NOT_IN_PLAN",
            status=403,
        )
    c = s.cluster
    if c is not None and c.role == "follower":
        got = (await c.forward("audit", query)).get("result")
        return got if isinstance(got, dict) else {}
    if c is not None and c.role == "head" and s.gathered is not None:
        s.gathered.catch_up()
        return s.gathered.query(query)
    return s.audit.query(query)


def clustered(s: Services) -> dict[str, Any]:
    """`GET /v1/cluster` and `cluster_status`."""
    return s.cluster.view() if s.cluster is not None else {"role": "standalone"}


def setup(app: web.Application, s: Services) -> None:
    def licence() -> dict[str, Any]:
        return s.licence.view()

    async def healthz(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def readyz(_: web.Request) -> web.Response:
        """Open: 200 while this node may take traffic (standalone and up, or a member in step with its head); 503
        with the reason otherwise, which keeps it out of the load balancer."""
        ready, reason, code = s.cluster.ready() if s.cluster is not None else (True, None, None)
        if ready:
            return web.json_response({"ready": True})
        return web.json_response({"ready": False, "reason": reason, "code": code}, status=503)

    async def cluster(_: web.Request) -> web.Response:
        return web.json_response(clustered(s))

    async def health(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "version": VERSION,
                "mode": s.config.mode,
                "runtime_id": s.credentials.runtime_id,
                "link": s.link.view(),
                "sandbox": s.layers.to_json(),
                "parts": Parts.view(s.parts.installed()),
                "licence": licence(),
                "heartbeat": s.heartbeat.view() if s.heartbeat is not None else None,
                "cluster": s.cluster.short() if s.cluster is not None else {"role": "standalone"},
                "access": s.access.view() if s.access is not None else None,
                **drains.view(s.drain),
                "approvals": len(s.runner.approvals()),  # this node's
                "programs": len(s.programs.names()),
            }
        )

    async def runtime_token(request: web.Request) -> web.Response:
        data = await body(request)
        s.credentials.set(data.get("runtime_id"), data.get("token"))  # type: ignore[arg-type]
        return web.json_response({"runtime_id": s.credentials.runtime_id, "link": "connecting"})

    async def programs(_: web.Request) -> web.Response:
        return web.json_response({"programs": s.programs.describe()})

    async def deploy(request: web.Request) -> web.Response:
        return web.json_response(await ops.deploy(s, await body(request)), status=201)

    async def secrets_list(request: web.Request) -> web.Response:
        return web.json_response(await ops.secrets_list(s, request.match_info["name"]))

    async def secret_set(request: web.Request) -> web.Response:
        m, data = request.match_info, await body(request)
        return web.json_response(await ops.secret_set(s, m["name"], m["secret"], data.get("value")))

    async def secret_delete(request: web.Request) -> web.Response:
        m = request.match_info
        return web.json_response(await ops.secret_delete(s, m["name"], m["secret"]))

    async def graph_schema(_: web.Request) -> web.Response:
        return web.json_response(await ops.graph(s, "schema", {}))

    async def graph_op(request: web.Request) -> web.Response:
        """One operation of the graph by its name (search, node, nodes, neighbours, upsert, delete), its arguments
        the body (docs/architecture.md, 5.9)."""
        return web.json_response(await ops.graph(s, request.match_info["op"], await body(request)))

    async def schedules(_: web.Request) -> web.Response:
        return web.json_response(await ops.schedules(s))

    def pausing(paused: bool) -> Handler:
        async def handler(request: web.Request) -> web.Response:
            m = request.match_info
            return web.json_response(await ops.schedule_pause(s, m["program"], m["name"], paused))

        return handler

    async def call(request: web.Request) -> web.Response:
        return web.json_response(await ops.call(s, await body(request)))

    async def clis_list(_: web.Request) -> web.Response:
        return web.json_response(await ops.clis_list(s))

    async def cli_run(request: web.Request) -> web.Response:
        data = await body(request)
        return web.json_response(await ops.cli_run(s, {**data, "cli": request.match_info["name"]}))

    async def cli_login(request: web.Request) -> web.Response:
        return web.json_response(await ops.cli_login(s, {"cli": request.match_info["name"]}))

    async def resume(request: web.Request) -> web.Response:
        data = await body(request)
        return web.json_response(await ops.resume(s, request.match_info["run_id"], data.get("answer")))

    async def cancel(request: web.Request) -> web.Response:
        return web.json_response(await ops.cancel(s, request.match_info["run_id"]))

    async def run_status(request: web.Request) -> web.Response:
        return web.json_response(await ops.run_status(s, request.match_info["run_id"]))

    async def run_log(request: web.Request) -> web.Response:
        return web.json_response(await ops.run_log(s, request.match_info["run_id"]))

    async def approvals(_: web.Request) -> web.Response:
        return web.json_response({"approvals": await ops.approvals(s)})

    async def parts_list(_: web.Request) -> web.Response:
        return web.json_response({"parts": Parts.view(s.parts.installed())})

    async def part_install(request: web.Request) -> web.Response:
        data = await body(request)
        name, version, sha256, signature = (
            data.get("name"),
            data.get("version"),
            data.get("sha256"),
            data.get("signature"),
        )
        archive, url = data.get("archive"), data.get("url")
        if not all(isinstance(v, str) for v in (name, version, sha256, signature)) or not (
            isinstance(archive, str) or isinstance(url, str)
        ):
            raise BadRequest("Send {name, version, sha256, signature, archive (base64) | url}.")
        if isinstance(archive, str):
            try:
                raw = base64.b64decode(archive, validate=True)
            except (binascii.Error, ValueError):
                raise BadRequest("archive is the part's tarball in base64.", code="E_PART_ARCHIVE") from None
        elif s.fetch is None:
            raise BadRequest("This rrotor takes a part as archive (base64), not by url.", code="E_PART_ARCHIVE")
        else:
            raw = await s.fetch(str(url))
        installed = s.parts.install(str(name), str(version), raw, str(sha256), str(signature))
        return web.json_response(Parts.view([installed])[0], status=201)

    async def part_remove(request: web.Request) -> web.Response:
        s.parts.remove(request.match_info["name"])
        return web.json_response({"removed": request.match_info["name"]})

    async def audit(request: web.Request) -> web.Response:
        return web.json_response(await audited(s, dict(request.query)))

    async def licence_get(_: web.Request) -> web.Response:
        return web.json_response({"licence": licence()})

    async def licence_put(request: web.Request) -> web.Response:
        """The licence key as the body (or a JSON string); release N also takes a legacy licence: {payload,
        signature}, {token} or its offline token."""
        text = (await request.text()).strip()
        try:
            sent = json.loads(text)
        except json.JSONDecodeError:
            sent = text
        kept = s.licence.take(sent)
        return web.json_response({"licence": licence(), "kept": kept})

    async def apps_list(_: web.Request) -> web.Response:
        return web.json_response(await ops.apps_list(s))

    async def app_publish(request: web.Request) -> web.Response:
        return web.json_response(await ops.app_publish(s, await body(request)), status=201)

    def served() -> apps.Apps:
        if s.catalogue is None:
            raise Unavailable("This rrotor keeps no apps.", code="E_APPS")
        return apps.Apps(s.programs, s.catalogue, s.config.address)

    async def app_page(request: web.Request) -> web.Response:
        """The app's page or one of its files, to a browser the gate let in (docs/architecture.md, 5.8); a
        refusal is a page too, since a browser is reading."""
        name, path = request.match_info["name"], request.match_info.get("path", "")
        asked = request.query.get("version")
        roles = viewer_roles(current().actor)
        try:
            if asked is not None and not asked.isdigit():
                raise BadRequest("version is a number.", code="E_INPUTS")
            version = served().serving(name, roles, int(asked) if asked else None)
            data, kind = apps.Apps.file(version, path)
        except apps.AppError as err:
            page = apps.refused_page(err, signed_in=roles is not None)
            return web.Response(text=page, status=err.status, content_type="text/html", headers={"X-Error": err.code})
        headers = {
            "Content-Security-Policy": apps.CSP,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if kind in apps.INLINE:  # a PDF or an SVG opened as it is: the browser's own viewer, never a download
            headers["Content-Disposition"] = "inline"
        return web.Response(body=data, content_type=kind, headers=headers)

    async def app_call(request: web.Request) -> web.Response:
        """The page's call into one of the app's functions, as the viewer: the run's first Result."""
        name, function = request.match_info["name"], request.match_info["function"]
        version = served().serving(name, viewer_roles(current().actor))
        apps.Apps.check_call(version, function)
        inputs = apps.Apps.inputs_of(await request.read())
        data = {"program": name, "function": function, "inputs": inputs, "version": version.version}
        return web.json_response(await ops.call(s, data, caller="app", origin="app"))

    async def app_continue(request: web.Request) -> web.Response:
        """The page's answer to a run of the app waiting for input or an approval."""
        name, run_id = request.match_info["name"], request.match_info["run_id"]
        served().serving(name, viewer_roles(current().actor))
        run = s.runner.get(run_id)
        if run.program != name:
            raise NotFound(f"{name} has no run {run_id}.", code="E_RUN_MISSING")
        data = await body(request)
        return web.json_response(await ops.resume(s, run_id, data.get("answer")))

    async def auth_start(request: web.Request) -> web.Response:
        assert s.browser is not None  # noqa: S101  (registered only with one)
        return await s.browser.start(request)

    async def auth_callback(request: web.Request) -> web.Response:
        assert s.browser is not None  # noqa: S101
        return await s.browser.callback(request)

    async def auth_logout(request: web.Request) -> web.Response:
        assert s.browser is not None  # noqa: S101
        return await s.browser.logout(request)

    async def resource_metadata(request: web.Request) -> web.Response:
        """RFC 9728: this runtime as an OAuth protected resource, and Glyphh as the server that signs a client in."""
        return web.json_response(
            {
                "resource": f"{public_url(request, s.config.public_host)}/mcp",
                "authorization_servers": [s.config.server.rstrip("/")],
                "scopes_supported": [SCOPE],
                "bearer_methods_supported": ["header"],
                "resource_name": "Glyphh runtime",
            }
        )

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    app.router.add_get("/v1/cluster", cluster)
    app.router.add_get(METADATA, resource_metadata)
    app.router.add_get(METADATA + "/mcp", resource_metadata)
    app.router.add_get("/v1/health", health)
    app.router.add_put("/v1/runtime/token", local_only(s.config.mode, runtime_token))
    app.router.add_get("/v1/programs", programs)
    app.router.add_post("/v1/programs/deploy", deploy)
    app.router.add_get("/v1/programs/{name}/secrets", secrets_list)
    app.router.add_put("/v1/programs/{name}/secrets/{secret}", secret_set)
    app.router.add_delete("/v1/programs/{name}/secrets/{secret}", secret_delete)
    app.router.add_get("/v1/graph", graph_schema)
    app.router.add_post("/v1/graph/{op}", graph_op)
    app.router.add_get("/v1/schedules", schedules)
    app.router.add_post("/v1/schedules/{program}/{name}/pause", pausing(True))
    app.router.add_post("/v1/schedules/{program}/{name}/resume", pausing(False))
    app.router.add_get("/v1/apps", apps_list)
    app.router.add_post("/v1/apps", app_publish)
    app.router.add_post("/apps/{name}/call/{function}", app_call)
    app.router.add_post("/apps/{name}/runs/{run_id}/continue", app_continue)
    app.router.add_get("/apps/{name}/", app_page)
    app.router.add_get("/apps/{name}/{path:.+}", app_page)
    if s.browser is not None:
        app.router.add_get("/auth/start", auth_start)
        app.router.add_get("/auth/callback", auth_callback)
        app.router.add_post("/auth/logout", auth_logout)
    app.router.add_get("/v1/clis", clis_list)
    app.router.add_post("/v1/clis/{name}/run", cli_run)
    app.router.add_post("/v1/clis/{name}/login", cli_login)
    app.router.add_post("/v1/runs", call)
    app.router.add_post("/v1/runs/{run_id}/continue", resume)
    app.router.add_post("/v1/runs/{run_id}/cancel", cancel)
    app.router.add_get("/v1/runs/{run_id}", run_status)
    app.router.add_get("/v1/runs/{run_id}/log", run_log)
    app.router.add_get("/v1/approvals", approvals)
    app.router.add_get("/v1/parts", parts_list)
    app.router.add_post("/v1/parts", local_only(s.config.mode, part_install))
    app.router.add_delete("/v1/parts/{name}", local_only(s.config.mode, part_remove))
    app.router.add_get("/v1/audit", audit)
    app.router.add_get("/v1/licence", licence_get)
    app.router.add_put("/v1/licence", local_only(s.config.mode, licence_put))
    app.router.add_post("/mcp", mcp.route(s))
