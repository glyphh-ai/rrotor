"""The local API's routes: the same operations the link carries, for the mods' tools (docs/architecture.md, 7)."""

from __future__ import annotations

import base64
import binascii
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from aiohttp import web

from rrotord import VERSION
from rrotord.config import Config
from rrotord.deploy import Deployer
from rrotord.errors import BadRequest
from rrotord.link.credentials import Credentials
from rrotord.programs import Programs, Version
from rrotord.runner.engine import Runner
from rrotord.runner.record import Run
from rrotord.sandbox.probe import Layers

BUNDLE_MAX = 64 * 1024 * 1024
BODY_MAX = 96 * 1024 * 1024  # a bundle in base64, with room


class LinkView(Protocol):
    def view(self) -> dict[str, Any]: ...


@dataclass(slots=True)
class Services:
    config: Config
    programs: Programs
    deployer: Deployer
    runner: Runner
    credentials: Credentials
    link: LinkView
    layers: Layers
    browser: Any = None  # the browser part's state, when it is in this image


async def body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise BadRequest("The body must be JSON.") from None
    if not isinstance(data, dict):
        raise BadRequest("The body must be a JSON object.")
    return data


def answer(run: Run) -> dict[str, Any]:
    result = run.result or {}
    return {
        "run_id": run.id,
        "program": run.program,
        "version": run.version,
        "function": run.function,
        "status": run.status,
        "data": result.get("data"),
        "step": result.get("step"),
        "prompt": result.get("prompt"),
        "seconds": round(run.seconds, 3),
    }


def setup(app: web.Application, s: Services) -> None:
    async def healthz(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def health(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "version": VERSION,
                "mode": s.config.mode,
                "runtime_id": s.credentials.runtime_id,
                "link": s.link.view(),
                "sandbox": s.layers.to_json(),
                "browser": s.browser.view()
                if s.browser is not None
                else {"ready": False, "detail": "not in this image"},
                "approvals": len(s.runner.approvals()),
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
        data = await body(request)
        commit, bundle, repo, token = data.get("commit"), data.get("bundle"), data.get("repo"), data.get("token")
        if not isinstance(commit, str):
            raise BadRequest("Send {bundle, commit} (a git bundle, base64) or {repo, commit, token}.")
        if isinstance(bundle, str):
            version = await deploy_bundle(bundle, commit)
        elif isinstance(repo, str) and (token is None or isinstance(token, str)):
            version = await s.deployer.from_repo(repo, commit, token)
        else:
            raise BadRequest("Send {bundle, commit} (a git bundle, base64) or {repo, commit, token}.")
        return web.json_response(version.to_json(), status=201)

    async def deploy_bundle(encoded: str, commit: str) -> Version:
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise BadRequest("bundle is a git bundle in base64.", code="E_BUNDLE") from None
        if len(raw) > BUNDLE_MAX:
            raise BadRequest("The bundle is over 64 MB.", code="E_TOO_BIG")
        folder = s.config.run_dir / "bundles"
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=folder, suffix=".bundle", delete=False) as f:
            f.write(raw)
            path = Path(f.name)
        try:
            return await s.deployer.from_bundle(path, commit)
        finally:
            path.unlink(missing_ok=True)

    async def call(request: web.Request) -> web.Response:
        data = await body(request)
        program, function = data.get("program"), data.get("function", "start")
        inputs, version = data.get("inputs") or {}, data.get("version")
        if (
            not isinstance(program, str)
            or not isinstance(function, str)
            or not isinstance(inputs, dict)
            or not (version is None or isinstance(version, int))
        ):
            raise BadRequest("Send {program, function, inputs, version?}.")
        run = await s.runner.call(program=program, function=function, inputs=inputs, version=version, caller="claude")
        return web.json_response(answer(run))

    async def resume(request: web.Request) -> web.Response:
        data = await body(request)
        run = await s.runner.resume(request.match_info["run_id"], data.get("answer"))
        return web.json_response(answer(run))

    async def cancel(request: web.Request) -> web.Response:
        return web.json_response(answer(await s.runner.cancel(request.match_info["run_id"])))

    async def run_status(request: web.Request) -> web.Response:
        return web.json_response(s.runner.get(request.match_info["run_id"]).view())

    async def run_log(request: web.Request) -> web.Response:
        run = s.runner.get(request.match_info["run_id"])
        return web.json_response({"run_id": run.id, "lines": s.runner.records.lines(run.id)})

    async def approvals(_: web.Request) -> web.Response:
        return web.json_response({"approvals": s.runner.approvals()})

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/v1/health", health)
    app.router.add_put("/v1/runtime/token", runtime_token)
    app.router.add_get("/v1/programs", programs)
    app.router.add_post("/v1/programs/deploy", deploy)
    app.router.add_post("/v1/runs", call)
    app.router.add_post("/v1/runs/{run_id}/continue", resume)
    app.router.add_post("/v1/runs/{run_id}/cancel", cancel)
    app.router.add_get("/v1/runs/{run_id}", run_status)
    app.router.add_get("/v1/runs/{run_id}/log", run_log)
    app.router.add_get("/v1/approvals", approvals)
