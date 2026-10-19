"""The API's side of audit and drains: who each request was for (the local token, a key's SHA-256, a sign-in's
subject, from where, by which route); every 401 and 403 an event; `GET /v1/audit` and `audit_query` on Enterprise
only; the health's drains and server-side logging; a span per request, the caller's traceparent honoured."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from rrotord.access import Access
from rrotord.access import digest as sha
from rrotord.api.app import make
from rrotord.audit import Audit
from rrotord.drain import Drain, SendFailed
from tests.daemon.api.conftest import PORT, Api, open_api, services
from tests.daemon.api.test_hosted import HOST, MACHINE, Vouches
from tests.fakes import DONE_MODEL, GitRepo, install, manifest, token

LOCAL = {"Host": f"127.0.0.1:{PORT}"}


class Tier:
    def __init__(self, tier: str = "enterprise") -> None:
        self.tier = tier

    def __call__(self) -> str:
        return self.tier


@pytest.fixture
async def audited(tmp_path: Path, short_dir: Path) -> AsyncIterator[tuple[Api, Tier]]:
    api = await open_api(tmp_path, short_dir)
    await api.client.close()
    tier = Tier()
    api.services.audit = Audit(tmp_path / "audit", "rrotor-1", tier=tier)
    api.services.runner.audit = api.services.audit
    api.client = TestClient(TestServer(make(api.services)), headers=LOCAL)
    await api.client.start_server()
    yield api, tier
    await api.services.runner.shutdown()
    await api.client.close()


def events(api: Api) -> list[dict[str, Any]]:
    assert api.services.audit is not None
    return api.services.audit.read(None, 1000)[0]


async def test_the_audit_log_is_read_on_enterprise_only(audited):
    api, tier = audited
    install(api.services.models, manifest("sample"), DONE_MODEL)
    res = await api.client.post("/v1/runs", json={"model": "sample", "inputs": {"q": 1}}, headers=api.auth())
    run_id = (await res.json())["run_id"]
    res = await api.client.get("/v1/audit", params={"kind": "run.call", "limit": "5"}, headers=api.auth())
    body = await res.json()
    assert res.status == 200 and body["cursor"] is None
    [call] = body["events"]
    assert (call["run_id"], call["actor"], call["client"], call["via"]) == (
        run_id,
        {"type": "local"},
        "127.0.0.1",
        "rest",
    )
    res = await api.client.get("/v1/audit", params={"limit": "0"}, headers=api.auth())
    assert res.status == 400
    tier.tier = "pro"
    res = await api.client.get("/v1/audit", headers=api.auth())
    assert (res.status, (await res.json())["error"]["code"]) == (403, "E_NOT_IN_PLAN")


async def test_without_an_audit_log_the_route_is_not_in_plan(api):
    res = await api.client.get("/v1/audit", headers=api.auth())
    assert (res.status, (await res.json())["error"]["code"]) == (403, "E_NOT_IN_PLAN")


async def rpc(api: Api, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    res = await api.client.post("/mcp", json=message, headers=api.auth())
    return dict((await res.json())["result"])


async def test_mcp_calls_are_audited_as_mcp_and_audit_query_reads_them(audited):
    api, tier = audited
    install(api.services.models, manifest("sample"), DONE_MODEL)
    await rpc(api, "model_call", {"model": "sample"})
    found = await rpc(api, "audit_query", {"model": "sample", "limit": 10})
    assert not found["isError"] and [e["via"] for e in found["structuredContent"]["events"]] == ["mcp", "mcp"]
    tier.tier = "team"
    refused = await rpc(api, "audit_query", {})
    assert refused["isError"] and refused["structuredContent"]["error"]["code"] == "E_NOT_IN_PLAN"


async def test_deploys_and_their_failures_are_audited(audited, tmp_path):
    api, _ = audited
    repo = GitRepo(tmp_path / "src")
    sha_ = repo.model(manifest("sample"))
    repo.bare(tmp_path / "github", "owner/sample")
    res = await api.client.post("/v1/models/deploy", json={"repo": "owner/sample", "commit": sha_}, headers=api.auth())
    assert res.status == 201
    res = await api.client.post("/v1/models/deploy", json={"bundle": "%%%", "commit": sha_}, headers=api.auth())
    assert res.status == 400
    res = await api.client.post("/v1/models/deploy", json={"commit": sha_}, headers=api.auth())
    assert res.status == 400
    deployed, *failed = [e for e in events(api) if e["kind"].startswith("model.")]
    assert (deployed["kind"], deployed["model"], deployed["version"], deployed["repo"]) == (
        "model.deploy",
        "sample",
        1,
        "owner/sample",
    )
    assert [(e["kind"], e["outcome"]["code"]) for e in failed] == [
        ("model.deploy_failed", "E_BUNDLE"),
        ("model.deploy_failed", "E_BAD_REQUEST"),
    ]


async def test_every_401_and_403_is_an_audit_event_without_the_bearer(audited):
    api, _ = audited
    wrong = token()
    assert (await api.client.get("/v1/models", headers={"Authorization": f"Bearer {wrong}"})).status == 401
    assert (await api.client.get("/v1/models")).status == 401
    assert (await api.client.get("/v1/models", headers={**api.auth(), "Origin": "https://evil.example"})).status == 403
    assert (await api.client.get("/healthz")).status == 200
    denied = [e for e in events(api) if e["kind"] == "access.denied"]
    assert [(e["actor"], e["outcome"]["code"]) for e in denied] == [
        ({"type": "anonymous", "key_sha256": sha(wrong)}, "E_AUTH"),
        ({"type": "anonymous", "key_sha256": None}, "E_AUTH"),
        ({"type": "anonymous", "key_sha256": sha(api.token)}, "E_ORIGIN"),
    ]
    assert denied[0]["path"] == "/v1/models" and denied[0]["method"] == "GET" and denied[0]["outcome"]["status"] == 401
    assert all(wrong not in str(e) and api.token not in str(e) for e in events(api))


@pytest.fixture
async def hosted(tmp_path: Path, short_dir: Path) -> AsyncIterator[Api]:
    key = token()
    s = services(tmp_path, short_dir, token())
    s.config = dataclasses.replace(s.config, mode="hosted", machine_id=MACHINE, local_token_sha256=None)
    s.access = Access(s.config.data, (sha(key),))
    s.audit = Audit(tmp_path / "audit", tier=lambda: "enterprise")
    s.runner.audit = s.audit
    s.introspect = Vouches("signed-in-" + "x" * 20)  # type: ignore[assignment]
    client: TestClient[web.Request, web.Application] = TestClient(TestServer(make(s)), headers={"Host": HOST})
    await client.start_server()
    yield Api(client, s, key, tmp_path)
    await s.runner.shutdown()
    await client.close()


async def test_a_key_and_a_sign_in_are_named_with_the_forwarded_client(hosted):
    install(hosted.services.models, manifest("sample"), DONE_MODEL)
    forwarded = {"X-Forwarded-For": "203.0.113.9, 10.0.4.17"}
    await hosted.client.post("/v1/runs", json={"model": "sample"}, headers=hosted.auth(**forwarded))
    signed = {"Authorization": "Bearer signed-in-" + "x" * 20}
    await hosted.client.post("/v1/runs", json={"model": "sample"}, headers=signed)
    res = await hosted.client.put("/v1/licence", data="x", headers=hosted.auth())
    assert res.status == 403
    calls = [e for e in events(hosted) if e["kind"] == "run.call"]
    assert [(c["actor"], c["client"]) for c in calls] == [
        ({"type": "key", "key_sha256": sha(hosted.token)}, "10.0.4.17"),
        ({"type": "sign-in", "sub": "user_1"}, "127.0.0.1"),
    ]
    local_only = [e for e in events(hosted) if e["kind"] == "access.denied"][-1]
    assert (local_only["actor"]["type"], local_only["outcome"]["code"]) == ("key", "E_LOCAL_ONLY")


async def test_the_health_says_where_logs_go(api):
    res = await api.client.get("/v1/health", headers=api.auth())
    body = await res.json()
    assert body["drains"]["logs"] == {"state": "off"} and body["server_logs"] == {"on": False, "warning": None}


class Holds:
    """A destination that takes nothing: what was sent stays in the buffers, to be read."""

    async def send(self, signal: str, body: bytes) -> None:
        raise SendFailed("held")

    async def close(self) -> None:
        pass


async def test_each_request_is_a_span_under_the_callers_traceparent(api):
    s = api.services
    s.config = dataclasses.replace(s.config, otel=(("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://c/v1/traces"),))
    s.drain = Drain(s.config, runtime_id=None, collector=Holds())
    client = TestClient(TestServer(make(s)), headers=LOCAL)
    await client.start_server()
    try:
        parent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        res = await client.get("/v1/runs/nope", headers={**api.auth(), "traceparent": parent})
        assert res.status == 404
        await client.get("/no/such/route", headers=api.auth())
        body = await (await client.get("/v1/health", headers=api.auth())).json()
    finally:
        await client.close()
        await s.drain.stop()
    assert s.drain.collector is not None
    spans = list(s.drain.collector.buffers["traces"].items)
    assert [x.name for x in spans] == ["GET /v1/runs/{run_id}", "GET /no/such/route", "GET /v1/health"]
    assert spans[0].context.trace_id == int("0af7651916cd43dd8448eb211c80319c", 16)
    assert spans[0].attributes["http.response.status_code"] == 404
    assert body["drains"]["traces"]["state"] in ("waiting", "failing") and body["drains"]["logs"] == {"state": "off"}
