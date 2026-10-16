"""The hosted gate: a runtime key at this machine's own address; another machine's address replayed; a paused
rrotor answering 402; health open."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from rrotord.access import Access, digest
from rrotord.api.app import make
from tests.daemon.api.conftest import Api, services
from tests.fakes import token

MACHINE = "3d8f1a2b3c4d5e"
HOST = f"{MACHINE}.glyphh.app"


@pytest.fixture
async def hosted(tmp_path: Path, short_dir: Path) -> AsyncIterator[Api]:
    key = token()
    s = services(tmp_path, short_dir, token())
    s.config = dataclasses.replace(s.config, mode="hosted", machine_id=MACHINE, local_token_sha256=None)
    s.access = Access(s.config.data, (digest(key),))
    client: TestClient[web.Request, web.Application] = TestClient(TestServer(make(s)), headers={"Host": HOST})
    await client.start_server()
    yield Api(client, s, key, tmp_path)
    await s.runner.shutdown()
    await client.close()


async def code_of(api: Api, path: str = "/v1/health", **headers: str) -> tuple[int, str | None]:
    res = await api.client.get(path, headers=headers)
    body = await res.json() if res.content_type == "application/json" else {}
    return res.status, body.get("error", {}).get("code") if isinstance(body, dict) else None


async def test_a_runtime_key_at_this_machines_address_lets_a_call_in(hosted):
    assert (await code_of(hosted, **hosted.auth()))[0] == 200
    assert (await code_of(hosted, **hosted.auth(Host=f"{MACHINE.upper()}.Glyphh.App:443")))[0] == 200
    res = await hosted.client.get("/v1/health", headers=hosted.auth())
    assert (await res.json())["access"] == {"keys": 1, "paused": None}
    assert (await code_of(hosted, **hosted.auth(Origin="https://claude.ai")))[
        0
    ] == 200  # no Origin rule: the key is the gate
    assert await code_of(hosted) == (401, "E_AUTH")
    assert await code_of(hosted, Authorization=f"Bearer {token()}") == (401, "E_AUTH")
    assert (await code_of(hosted, "/healthz", Host="whatever.example.com"))[0] == 200


async def test_another_machines_address_is_replayed_and_the_rest_is_misdirected(hosted):
    res = await hosted.client.post("/v1/runs", headers={"Host": "a1b2c3d4e5f6a7.glyphh.app"}, json={})
    assert res.status == 200 and res.headers["fly-replay"] == "instance=a1b2c3d4e5f6a7"
    for host in ("glyphh.app", "evil.example.com", f"{MACHINE}.glyphh.app.evil.com", "127.0.0.1:7777"):
        assert await code_of(hosted, **hosted.auth(Host=host)) == (421, "E_HOST"), host


async def test_a_paused_runtime_answers_402_to_everything_but_its_health(hosted):
    hosted.services.access.pause("runtime_hours")
    res = await hosted.client.get("/v1/models", headers=hosted.auth())
    body = await res.json()
    assert (res.status, body["error"]["code"], body["error"]["limit"]) == (402, "E_LIMIT", "runtime_hours")
    assert (await code_of(hosted, "/healthz"))[0] == 200
    hosted.services.access.resume()
    assert (await code_of(hosted, "/v1/models", **hosted.auth()))[0] == 200
