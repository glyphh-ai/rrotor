from __future__ import annotations

import dataclasses
import hashlib

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from rrotord.access import Access
from rrotord.api.errors import errors
from rrotord.api.gate import gate, machine_of, same
from tests.daemon.api.conftest import PORT, Api
from tests.fakes import token


def test_same_compares_by_sha256():
    import hashlib

    secret = token()
    assert same(secret, hashlib.sha256(secret.encode()).hexdigest())
    assert not same(secret, "0" * 64)


async def code_of(api: Api, path: str = "/v1/health", **headers: str) -> tuple[int, str | None]:
    res = await api.client.get(path, headers=headers)
    body = await res.json()
    return res.status, body.get("error", {}).get("code") if isinstance(body, dict) else None


async def test_healthz_is_open(api):
    res = await api.client.get("/healthz", headers={"Host": "evil.example.com"})
    assert res.status == 200 and await res.json() == {"ok": True}


async def test_the_local_token_lets_a_call_in(api):
    assert (await code_of(api, **api.auth()))[0] == 200
    assert (await code_of(api, **api.auth(Host=f"localhost:{PORT}")))[0] == 200


async def test_a_host_other_than_loopback_is_refused(api):
    assert await code_of(api, **api.auth(Host="evil.example.com")) == (403, "E_HOST")


async def test_a_foreign_origin_is_refused(api):
    assert await code_of(api, **api.auth(Origin="https://evil.example.com")) == (403, "E_ORIGIN")
    assert (await code_of(api, **api.auth(Origin=f"http://127.0.0.1:{PORT}")))[0] == 200


async def test_no_token_or_a_wrong_one_is_refused(api):
    assert await code_of(api) == (401, "E_AUTH")
    assert await code_of(api, Authorization=f"Bearer {token()}") == (401, "E_AUTH")
    assert await code_of(api, Authorization="Basic abc") == (401, "E_AUTH")
    assert await code_of(api, Authorization="Bearer ") == (401, "E_AUTH")


async def test_only_a_bearer_token_is_taken(api):
    assert await code_of(api, **{"X-Token": api.token}) == (401, "E_AUTH")
    assert await code_of(api, f"/v1/health?t={api.token}") == (401, "E_AUTH")


def test_the_machine_a_host_names():
    assert machine_of("3d8f1a2b3c4d5e.glyphh.app", "glyphh.app") == "3d8f1a2b3c4d5e"
    assert machine_of("ABC123.Glyphh.App:443", "glyphh.app") == "abc123"
    for host in ("glyphh.app", "a.b.glyphh.app", "x.glyphh.app", "3d8f1a2b3c4d5e.other.app", "-bad-.glyphh.app", ""):
        assert machine_of(host, "glyphh.app") is None, host


async def test_without_a_local_token_nothing_is_let_in(api):
    async def ok(_: web.Request) -> web.Response:
        return web.json_response({})

    config = dataclasses.replace(api.services.config, local_token_sha256=None)
    app = web.Application(middlewares=[errors, gate(config, Access(config.data))])
    app.router.add_get("/x", ok)
    async with TestClient(TestServer(app), headers={"Host": f"127.0.0.1:{PORT}"}) as client:
        res = await client.get("/x", headers={"Authorization": f"Bearer {token()}"})
        assert res.status == 401


async def test_the_local_token_is_not_a_runtime_key_and_is_never_counted(api):
    async def ok(_: web.Request) -> web.Response:
        return web.json_response({})

    access = Access(api.services.config.data, (hashlib.sha256(api.token.encode()).hexdigest(),))
    app = web.Application(middlewares=[errors, gate(api.services.config, access)])
    app.router.add_get("/x", ok)
    async with TestClient(TestServer(app), headers={"Host": f"127.0.0.1:{PORT}"}) as client:
        res = await client.get("/x", headers=api.auth())
        assert res.status == 200
    assert access.usage.take() == []
