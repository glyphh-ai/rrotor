"""GET /v1/folders, the platform's folder picker read by the browser itself (rrotord/api/cors.py): the roots the
rrotor's owner offers and the folders inside them, for the platform's one origin, with the work ticket Glyphh signs
(rrotord/ticket.py) or any bearer the gate takes; and a call's `folders`, given to its run."""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from rrotord.access import Access, digest
from rrotord.api.app import make
from rrotord.folders import Folders
from tests.daemon.api.conftest import PORT, Api, services
from tests.fakes import LocalLauncher, b64url, install, keys, manifest, token

PLATFORM = "https://platform.glyphh.ai"
HOST = "3d8f1a2b3c4d5e.glyphh.app"
RUNTIME = "0b0e3a52-6a55-4d0b-9d52-0f6f4f1c2a11"
CORS = {"Access-Control-Allow-Origin": PLATFORM, "Vary": "Origin"}


def ticket(**fields: Any) -> str:
    payload = {"kind": "work", "runtime_id": RUNTIME, "sub": "user_1", "expires_at": "2999-01-01T00:00:00Z"} | fields
    raw = json.dumps(payload).encode()
    return f"{b64url(raw)}.{b64url(keys().private.sign(raw))}"


@dataclasses.dataclass
class Machine:
    api: Api
    docs: Path

    def auth(self, bearer: str | None = None, origin: str | None = PLATFORM) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {bearer or ticket()}"}
        return headers | ({"Origin": origin} if origin else {})

    async def get(self, path: str, **headers: str) -> tuple[int, Any, Any]:
        res = await self.api.client.get(path, headers=headers)
        return res.status, await res.json(), res.headers


async def machine(tmp_path: Path, short_dir: Path, hosted: bool) -> Machine:
    home = Path(os.path.realpath(tmp_path)) / "home"
    for folder in ("Documents/invoices", "Documents/letters", "Private"):
        (home / folder).mkdir(parents=True)
    secret = token()
    s = services(tmp_path, short_dir, secret)
    s.runner.folders = Folders((home / "Documents",))
    s.config = dataclasses.replace(s.config, runtime_id=RUNTIME)
    host = f"127.0.0.1:{PORT}"
    if hosted:
        s.config = dataclasses.replace(s.config, mode="hosted", machine_id=HOST.split(".")[0], local_token_sha256=None)
        s.access = Access(s.config.data, (digest(secret),))
        host = HOST
    client: TestClient[web.Request, web.Application] = TestClient(TestServer(make(s)), headers={"Host": host})
    await client.start_server()
    return Machine(Api(client, s, secret, tmp_path), home / "Documents")


@pytest.fixture
async def local(tmp_path: Path, short_dir: Path) -> AsyncIterator[Machine]:
    m = await machine(tmp_path, short_dir, hosted=False)
    yield m
    await m.api.services.runner.shutdown()
    await m.api.client.close()


@pytest.fixture
async def hosted(tmp_path: Path, short_dir: Path) -> AsyncIterator[Machine]:
    m = await machine(tmp_path, short_dir, hosted=True)
    yield m
    await m.api.services.runner.shutdown()
    await m.api.client.close()


async def test_the_picker_lists_the_roots_then_the_folders_inside(local):
    docs = local.docs
    status, body, _ = await local.get("/v1/folders", **local.api.auth())
    assert (status, body) == (200, {"folders": [{"path": str(docs), "name": "Documents"}]})
    status, body, _ = await local.get(f"/v1/folders?under={docs}", **local.api.auth())
    assert (status, body) == (
        200,
        {
            "folders": [
                {"path": f"{docs}/invoices", "name": "invoices"},
                {"path": f"{docs}/letters", "name": "letters"},
            ]
        },
    )


async def test_the_listing_stays_inside_the_roots(local):
    docs = local.docs
    for under in (docs.parent, docs.parent / "Private", f"{docs}/..", f"{docs}/invoices/../..", "/", "/etc"):
        status, body, _ = await local.get(f"/v1/folders?under={under}", **local.api.auth())
        assert (status, body["error"]["code"]) == (403, "E_FOLDER_REFUSED"), under
        assert "is not in a folder this rrotor's owner shares with bots" in body["error"]["message"]
    status, body, _ = await local.get("/v1/folders?under=Documents", **local.api.auth())
    assert (status, body["error"]["code"]) == (400, "E_FOLDER")


async def test_the_platforms_page_reads_a_local_rrotor_with_its_ticket(local):
    status, body, headers = await local.get(f"/v1/folders?under={local.docs}", **local.auth())
    assert status == 200 and [f["name"] for f in body["folders"]] == ["invoices", "letters"]
    assert {k: headers[k] for k in CORS} == CORS
    # a refusal is readable by the page too
    status, body, headers = await local.get(f"/v1/folders?under={local.docs.parent}", **local.auth())
    assert (status, body["error"]["code"]) == (403, "E_FOLDER_REFUSED")
    assert headers["Access-Control-Allow-Origin"] == PLATFORM
    for bad in (token(), ticket(runtime_id="another"), ticket(expires_at="2020-01-01T00:00:00Z"), ticket(kind="x")):
        status, body, headers = await local.get("/v1/folders", **local.auth(bad))
        assert (status, body["error"]["code"], headers["Access-Control-Allow-Origin"]) == (401, "E_AUTH", PLATFORM)
    # the local token works from the platform's page as from anywhere
    assert (await local.get("/v1/folders", **local.api.auth(Origin=PLATFORM)))[0] == 200


async def test_the_preflight_is_answered_for_the_platform_alone(local):
    asks = {
        "Origin": PLATFORM,
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Private-Network": "true",
    }
    res = await local.api.client.options("/v1/folders", headers=asks)
    assert res.status == 204 and {k: res.headers[k] for k in CORS} == CORS
    assert res.headers["Access-Control-Allow-Methods"] == "GET"
    assert res.headers["Access-Control-Allow-Headers"] == "Authorization"
    assert res.headers["Access-Control-Allow-Private-Network"] == "true"
    for path, origin in (("/v1/folders", "https://evil.example.com"), ("/v1/programs", PLATFORM)):
        res = await local.api.client.options(path, headers=asks | {"Origin": origin})
        assert res.status == 403 and "Access-Control-Allow-Origin" not in res.headers, (path, origin)


async def test_a_ticket_lists_folders_and_does_nothing_else(local):
    # another origin's page, another path, another method: the ticket is nothing there
    assert (await local.get("/v1/folders", **local.auth(origin="https://evil.example.com")))[0] == 403
    status, body, headers = await local.get("/v1/programs", **local.auth())
    assert (status, body["error"]["code"]) == (403, "E_ORIGIN") and "Access-Control-Allow-Origin" not in headers
    for path in ("/v1/programs", "/v1/health", "/v1/runs"):
        assert (await local.get(path, **local.auth(origin=None)))[0] == 401, path
    res = await local.api.client.post("/v1/runs", headers=local.auth(origin=None), json={"program": "reader"})
    assert res.status == 401
    res = await local.api.client.post("/v1/folders", headers=local.auth(), json={})
    assert res.status == 403


async def test_the_platforms_page_reads_a_self_hosted_rrotor_with_its_ticket(hosted):
    status, body, headers = await hosted.get(f"/v1/folders?under={hosted.docs}", **hosted.auth())
    assert status == 200 and [f["name"] for f in body["folders"]] == ["invoices", "letters"]
    assert {k: headers[k] for k in CORS} == CORS
    # the ticket lists folders (and reads the work), and nothing else
    for path in ("/v1/programs", "/v1/health"):
        assert (await hosted.get(path, **hosted.auth()))[0] == 401, path
    res = await hosted.api.client.post("/v1/runs", headers=hosted.auth(), json={})
    assert res.status == 401
    assert (await hosted.get("/v1/folders", **hosted.auth(ticket(runtime_id="another"))))[0] == 401
    # a runtime key reads it as it reads everything
    assert (await hosted.get("/v1/folders", **hosted.api.auth()))[0] == 200


SEES = "from rrotor import Result\n\ndef start(ctx, inputs):\n    return Result.done(list(ctx.folders))\n"
WAITS = """
from rrotor import Result

def start(ctx, inputs):
    if ctx.ask_approval("write", "Write it?"):
        return Result.done(list(ctx.folders))
    return Result.done("declined")
"""


async def post(m: Machine, path: str, body: dict[str, Any]) -> tuple[int, Any]:
    res = await m.api.client.post(path, headers=m.api.auth(), json=body)
    return res.status, await res.json()


async def test_a_call_gives_its_run_the_folders_it_names(local):
    docs = str(local.docs)
    install(local.api.services.programs, manifest("reader", folders=True), SEES)
    asked = [{"path": f"{docs}/letters", "access": "write"}, {"path": docs, "access": "read"}]
    status, body = await post(local, "/v1/runs", {"program": "reader", "folders": asked})
    given = [{"path": docs, "access": "read"}, {"path": f"{docs}/letters", "access": "write"}]
    assert (status, body["data"]) == (200, given)  # told to the program, parents first
    launcher = cast(LocalLauncher, local.api.services.runner.launcher)
    assert launcher.specs[-1].mounts.folders == ((docs, False), (f"{docs}/letters", True))  # and mounted
    run = local.api.services.runner.records.load(body["run_id"])
    assert run is not None and run.folders == given
    # a call with none has none
    status, body = await post(local, "/v1/runs", {"program": "reader"})
    assert (status, body["data"]) == (200, []) and not launcher.specs[-1].mounts.folders


async def test_a_call_for_a_folder_not_offered_never_becomes_a_run(local):
    install(local.api.services.programs, manifest("reader", folders=True), SEES)
    install(local.api.services.programs, manifest("plain"), SEES, sha="b" * 40)
    private = str(local.docs.parent / "Private")
    status, body = await post(
        local, "/v1/runs", {"program": "reader", "folders": [{"path": private, "access": "read"}]}
    )
    assert (status, body["error"]["code"]) == (403, "E_FOLDER_REFUSED")
    assert body["error"]["message"] == (
        f"{private} is not in a folder this rrotor's owner shares with bots: it shares {local.docs}."
    )
    status, body = await post(local, "/v1/runs", {"program": "reader", "folders": "all"})
    assert (status, body["error"]["code"]) == (400, "E_FOLDER")
    # a program whose manifest takes no folders is given none
    asked = [{"path": str(local.docs), "access": "read"}]
    status, body = await post(local, "/v1/runs", {"program": "plain", "folders": asked})
    assert (status, body["error"]["code"]) == (403, "E_FOLDERS_UNDECLARED")
    assert body["error"]["message"] == 'plain takes no folders: its glyphh.json does not say "folders": true.'
    assert local.api.services.runner.records.all() == []


async def test_a_run_carried_on_after_its_folder_stopped_being_offered_fails_saying_so(local):
    s = local.api.services
    install(s.programs, manifest("reader", folders=True, functions={"start": {"approvals": ["write"]}}), WAITS)
    asked = [{"path": str(local.docs), "access": "write"}]
    status, body = await post(local, "/v1/runs", {"program": "reader", "folders": asked})
    assert (status, body["status"]) == (200, "needs_approval")
    await s.runner.shutdown()  # the rrotor restarts, its owner offering nothing now
    s.runner.folders = Folders()
    status, body = await post(local, f"/v1/runs/{body['run_id']}/continue", {"answer": {"approved": True}})
    assert (status, body["status"], body["step"]) == (200, "failed", "folders")
    assert body["data"]["code"] == "E_FOLDER_REFUSED" and "it shares none" in body["data"]["reason"]
