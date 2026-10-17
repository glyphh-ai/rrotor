"""The hosted gate: a runtime key at this machine's own address; another machine's address replayed; a paused
rrotor answering 402; health open."""

from __future__ import annotations

import dataclasses
import datetime as dt
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


async def test_in_development_one_exact_host_stands_in_for_the_machines_address(hosted):
    hosted.services.config = dataclasses.replace(hosted.services.config, public_host="dev-tunnel.ngrok-free.app")
    client: TestClient[web.Request, web.Application] = TestClient(TestServer(make(hosted.services)))
    await client.start_server()
    try:
        res = await client.get("/v1/health", headers={**hosted.auth(), "Host": "Dev-Tunnel.ngrok-free.app:443"})
        assert res.status == 200
        for host in (HOST, "a1b2c3d4e5f6a7.glyphh.app", "evil.example.com"):
            res = await client.get("/v1/health", headers={**hosted.auth(), "Host": host})
            assert res.status == 421 and "fly-replay" not in res.headers, host
    finally:
        await client.close()


class Vouches:
    """A fake of the Cloud's introspection: which tokens are signed in."""

    def __init__(self, *good: str) -> None:
        self.good = set(good)
        self.asked: list[str] = []

    async def active(self, token: str) -> bool:
        self.asked.append(token)
        return token in self.good


async def test_a_glyphh_sign_in_the_cloud_vouches_for_lets_a_call_in_and_a_refusal_says_where_to_sign_in(hosted):
    signed_in = token()
    hosted.services.introspect = Vouches(signed_in)
    client: TestClient[web.Request, web.Application] = TestClient(
        TestServer(make(hosted.services)), headers={"Host": HOST}
    )
    await client.start_server()
    try:
        res = await client.get("/v1/models", headers={"Authorization": f"Bearer {signed_in}"})
        assert res.status == 200
        res = await client.get("/v1/models", headers=hosted.auth())  # a runtime key never asks the Cloud
        assert res.status == 200 and hosted.services.introspect.asked == [signed_in]
        res = await client.get("/v1/models", headers={"Authorization": f"Bearer {token()}"})
        assert res.status == 401 and (await res.json())["error"]["code"] == "E_AUTH"
        metadata = (
            f'Bearer resource_metadata="https://{HOST}/.well-known/oauth-protected-resource/mcp", scope="account"'
        )
        assert res.headers["WWW-Authenticate"] == metadata + ', error="invalid_token"'
        res = await client.get("/v1/models", headers={"Host": f"{HOST}:443"})
        assert res.status == 401 and res.headers["WWW-Authenticate"] == (
            f'Bearer resource_metadata="https://{HOST}/.well-known/oauth-protected-resource/mcp", scope="account"'
        )
        for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
            res = await client.get(path)  # open: where a client finds how to sign in
            assert res.status == 200 and await res.json() == {
                "resource": f"https://{HOST}/mcp",
                "authorization_servers": ["https://glyphh.example.com"],
                "scopes_supported": ["account"],
                "bearer_methods_supported": ["header"],
                "resource_name": "Glyphh runtime",
            }
        res = await client.get(
            "/.well-known/oauth-protected-resource/mcp", headers={"Host": "a1b2c3d4e5f6a7.glyphh.app"}
        )
        assert res.headers["fly-replay"] == "instance=a1b2c3d4e5f6a7"  # another machine's metadata is its own
    finally:
        await client.close()


async def test_without_the_cloud_only_a_runtime_key_gets_in(hosted):
    assert hosted.services.introspect is None
    res = await hosted.client.get("/v1/models", headers={"Authorization": f"Bearer {token()}"})
    assert res.status == 401 and 'error="invalid_token"' in res.headers["WWW-Authenticate"]


async def test_only_the_requests_served_are_counted_for_the_key_that_let_them_in(hosted):
    access = hosted.services.access
    other = token()
    access.set_keys([digest(hosted.token), digest(other)])
    for _ in range(2):
        assert (await code_of(hosted, **hosted.auth()))[0] == 200
    assert (await code_of(hosted, "/v1/models", Authorization=f"Bearer {other}"))[0] == 200
    assert await code_of(hosted) == (401, "E_AUTH")
    assert await code_of(hosted, Authorization=f"Bearer {token()}") == (401, "E_AUTH")
    assert (await code_of(hosted, "/healthz"))[0] == 200  # open: no key, no call
    res = await hosted.client.get("/v1/health", headers=hosted.auth(Host="a1b2c3d4e5f6a7.glyphh.app"))
    assert res.headers["fly-replay"] == "instance=a1b2c3d4e5f6a7"  # served by that machine, counted there
    access.pause("runtime_hours")
    assert await code_of(hosted, **hosted.auth()) == (402, "E_LIMIT")
    access.resume()
    taken = access.usage.take()
    assert [(k["key_sha256"], k["calls"]) for k in taken] == [(digest(hosted.token), 2), (digest(other), 1)]
    for k in taken:
        assert dt.datetime.fromisoformat(k["last_used_at"]).utcoffset() == dt.timedelta(0)
    assert access.usage.take() == []
