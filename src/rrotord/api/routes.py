"""The API's routes (docs/architecture.md, 7): on loopback for a local rrotor's caller, at `<machine>.<domain>` for a
hosted one's callers, at its one public host for a self-hosted one's. The operations themselves are in ops.py, shared
with the MCP server (mcp.py).

What changes the runtime itself, not its models (its link's token, its parts, its licence), answers only on a local
rrotor, to its owner's mod with the local token. A hosted or self-hosted rrotor takes those from its environment, its
image and Glyphh (the link, the heartbeat, a licence key in GLYPHH_LICENSE_KEY or /etc/rrotor/license.key), never
from a caller: a runtime key or a Glyphh sign-in calls and deploys models, and is refused E_LOCAL_ONLY there."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from aiohttp import web

from rrotord import VERSION
from rrotord import drain as drains
from rrotord.access import Access
from rrotord.api import mcp, ops
from rrotord.api.gate import METADATA, SCOPE, public_url
from rrotord.audit import Audit
from rrotord.cluster.node import Cluster
from rrotord.config import Config
from rrotord.deploy import Deployer
from rrotord.drain import Drain
from rrotord.errors import BadRequest, Forbidden, RrotorError
from rrotord.introspect import Introspector
from rrotord.licence import Licence
from rrotord.link.credentials import Credentials
from rrotord.models import Models, Version
from rrotord.parts import Parts
from rrotord.precedents import Precedents
from rrotord.runner.engine import Runner
from rrotord.sandbox.probe import Layers

BODY_MAX = 96 * 1024 * 1024  # a bundle in base64, with room


Handler = Callable[[web.Request], Awaitable[web.Response]]


class Fetch(Protocol):
    async def __call__(self, url: str) -> bytes: ...


Report = Callable[[Version, str | None], Awaitable[None]]  # `deployed` up the link: the version and its repo, if any


class LinkView(Protocol):
    def view(self) -> dict[str, Any]: ...


class HeartbeatView(Protocol):
    def view(self) -> dict[str, Any] | None: ...


@dataclass(slots=True)
class Services:
    config: Config
    models: Models
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
    precedents: Precedents | None = None  # the daemon's nine-minute count; counted here when there is none
    audit: Audit | None = None  # the audit events: kept (Enterprise) and read by GET /v1/audit, or logged
    drain: Drain | None = None  # the customer's collector and server-side logging, when either is on
    cluster: Cluster | None = None  # this node's place in its cluster; none: standalone


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


def audited(s: Services, query: dict[str, Any]) -> dict[str, Any]:
    """`GET /v1/audit` and `audit_query`: the kept audit events, Enterprise only."""
    if s.audit is None or not s.audit.kept:
        raise RrotorError(
            "The audit log is part of the Enterprise plan; below it audit events are log lines (logger rrotor.audit).",
            code="E_NOT_IN_PLAN",
            status=403,
        )
    return s.audit.query(query)


def clustered(s: Services) -> dict[str, Any]:
    """`GET /v1/cluster` and `cluster_status`."""
    return s.cluster.view() if s.cluster is not None else {"role": "standalone"}


def setup(app: web.Application, s: Services) -> None:
    counted = s.precedents or Precedents(s.models)

    def licence() -> dict[str, Any]:
        return s.licence.view(counted.current())

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
                "approvals": len(s.runner.approvals()),
                "models": len(s.models.names()),
            }
        )

    async def runtime_token(request: web.Request) -> web.Response:
        data = await body(request)
        s.credentials.set(data.get("runtime_id"), data.get("token"))  # type: ignore[arg-type]
        return web.json_response({"runtime_id": s.credentials.runtime_id, "link": "connecting"})

    async def models(_: web.Request) -> web.Response:
        return web.json_response({"models": s.models.describe()})

    async def deploy(request: web.Request) -> web.Response:
        return web.json_response(await ops.deploy(s, await body(request)), status=201)

    async def call(request: web.Request) -> web.Response:
        return web.json_response(await ops.call(s, await body(request)))

    async def resume(request: web.Request) -> web.Response:
        data = await body(request)
        return web.json_response(await ops.resume(s, request.match_info["run_id"], data.get("answer")))

    async def cancel(request: web.Request) -> web.Response:
        return web.json_response(await ops.cancel(s, request.match_info["run_id"]))

    async def run_status(request: web.Request) -> web.Response:
        return web.json_response(ops.run_status(s, request.match_info["run_id"]))

    async def run_log(request: web.Request) -> web.Response:
        return web.json_response(ops.run_log(s, request.match_info["run_id"]))

    async def approvals(_: web.Request) -> web.Response:
        return web.json_response({"approvals": s.runner.approvals()})

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
        return web.json_response(audited(s, dict(request.query)))

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
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/models/deploy", deploy)
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
