from __future__ import annotations

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from rrotord.api.errors import errors
from rrotord.api.gate import gate, same
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


async def test_x_token_and_query_token_only_for_the_browser_routes(api):
    assert await code_of(api, **{"X-Token": api.token}) == (401, "E_AUTH")
    assert (await code_of(api, "/api/browser", **{"X-Token": api.token}))[0] == 404
    assert (await code_of(api, f"/api/browser?t={api.token}"))[0] == 404
    assert await code_of(api, "/api/browser") == (401, "E_AUTH")


async def test_without_a_local_token_nothing_is_let_in():
    async def ok(_: web.Request) -> web.Response:
        return web.json_response({})

    app = web.Application(middlewares=[errors, gate(None, (PORT,))])
    app.router.add_get("/x", ok)
    async with TestClient(TestServer(app), headers={"Host": f"127.0.0.1:{PORT}"}) as client:
        res = await client.get("/x", headers={"Authorization": f"Bearer {token()}"})
        assert res.status == 401
