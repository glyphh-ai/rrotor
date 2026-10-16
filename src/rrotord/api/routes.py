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
from rrotord.licence import Licence
from rrotord.link.credentials import Credentials
from rrotord.models import Models, Version
from rrotord.parts import BASE_MOD, Parts
from rrotord.runner.engine import Runner
from rrotord.runner.record import Run
from rrotord.sandbox.probe import Layers

BUNDLE_MAX = 64 * 1024 * 1024
BODY_MAX = 96 * 1024 * 1024  # a bundle in base64, with room


class Fetch(Protocol):
    async def __call__(self, url: str) -> bytes: ...


class LinkView(Protocol):
    def view(self) -> dict[str, Any]: ...


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
        "model": run.model,
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
                "parts": Parts.view(s.parts.installed()),
                "licence": s.licence.view(),
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
        model, function = data.get("model"), data.get("function", "start")
        inputs, version, clearance = data.get("inputs") or {}, data.get("version"), data.get("clearance", 1.0)
        if (
            not isinstance(model, str)
            or not isinstance(function, str)
            or not isinstance(inputs, dict)
            or not (version is None or isinstance(version, int))
            or isinstance(clearance, bool)
            or not isinstance(clearance, int | float)
            or not 0 <= clearance <= 1
        ):
            raise BadRequest("Send {model, function, inputs, version?, clearance? (0 to 1)}.")
        run = await s.runner.call(
            model=model, function=function, inputs=inputs, version=version, caller="claude", clearance=float(clearance)
        )
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
        info = Parts.read_info(raw) if raw[:2] == b"\x1f\x8b" else None
        if info is not None and info.mod != BASE_MOD:
            s.licence.require(info.mod)
        installed = s.parts.install(str(name), str(version), raw, str(sha256), str(signature))
        return web.json_response(Parts.view([installed])[0], status=201)

    async def part_remove(request: web.Request) -> web.Response:
        s.parts.remove(request.match_info["name"])
        return web.json_response({"removed": request.match_info["name"]})

    async def licence_get(_: web.Request) -> web.Response:
        return web.json_response({"licence": s.licence.view()})

    async def licence_put(request: web.Request) -> web.Response:
        data = await body(request)
        s.licence.set(data.get("payload"), data.get("signature"))
        return web.json_response({"licence": s.licence.view()})

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/v1/health", health)
    app.router.add_put("/v1/runtime/token", runtime_token)
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/models/deploy", deploy)
    app.router.add_post("/v1/runs", call)
    app.router.add_post("/v1/runs/{run_id}/continue", resume)
    app.router.add_post("/v1/runs/{run_id}/cancel", cancel)
    app.router.add_get("/v1/runs/{run_id}", run_status)
    app.router.add_get("/v1/runs/{run_id}/log", run_log)
    app.router.add_get("/v1/approvals", approvals)
    app.router.add_get("/v1/parts", parts_list)
    app.router.add_post("/v1/parts", part_install)
    app.router.add_delete("/v1/parts/{name}", part_remove)
    app.router.add_get("/v1/licence", licence_get)
    app.router.add_put("/v1/licence", licence_put)
