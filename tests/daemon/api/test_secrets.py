"""A program's secrets over REST and MCP: set, listed by name, deleted; never a value back, the audit naming the
secret only; refused for a program not here, a bad body, a follower, or a rrotor with no vault."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from rrotord.audit import Audit
from tests.daemon.api.conftest import Api
from tests.daemon.api.test_mcp import rpc, tool
from tests.daemon.api.test_routes import send
from tests.fakes import install, manifest

READS = "from rrotor import Result\n\ndef start(ctx, inputs):\n    return Result.done(len(ctx.secrets['API_KEY']))\n"
VALUE = "sk-live-0123456789abcdef"


def deploy(api: Api) -> None:
    install(api.services.programs, manifest("sample", secrets=["API_KEY", "UNSET"]), READS)


async def test_set_list_call_and_delete_over_rest(api, tmp_path):
    deploy(api)
    api.services.audit = Audit(tmp_path / "audit", "n1", tier=lambda: "enterprise")
    status, listed = await send(api, "GET", "/v1/programs/sample/secrets")
    assert (status, listed) == (
        200,
        {
            "program": "sample",
            "secrets": [
                {"name": "API_KEY", "declared": True, "set": False},
                {"name": "UNSET", "declared": True, "set": False},
            ],
        },
    )
    status, missing = await send(api, "POST", "/v1/runs", json={"program": "sample"})
    assert (missing["status"], missing["step"], missing["data"]["missing"]) == (
        "failed",
        "secrets",
        ["API_KEY", "UNSET"],
    )
    status, body = await send(api, "PUT", "/v1/programs/sample/secrets/API_KEY", json={"value": VALUE})
    assert (status, body) == (200, {"program": "sample", "name": "API_KEY", "set": True})
    await send(api, "PUT", "/v1/programs/sample/secrets/EXTRA", json={"value": "x"})
    status, listed = await send(api, "GET", "/v1/programs/sample/secrets")
    assert listed["secrets"] == [
        {"name": "API_KEY", "declared": True, "set": True},
        {"name": "EXTRA", "declared": False, "set": True},
        {"name": "UNSET", "declared": True, "set": False},
    ]
    assert VALUE not in str(listed)
    install(api.services.programs, manifest("sample", secrets=["API_KEY"]), READS, sha="b" * 40, version=2)
    status, done = await send(api, "POST", "/v1/runs", json={"program": "sample"})
    assert (done["status"], done["data"]) == ("done", len(VALUE)), done
    status, body = await send(api, "DELETE", "/v1/programs/sample/secrets/API_KEY")
    assert (status, body["set"]) == (200, False)
    status, body = await send(api, "DELETE", "/v1/programs/sample/secrets/API_KEY")
    assert (status, body["error"]["code"]) == (404, "E_SECRET_MISSING")
    events = api.services.audit.query({"limit": "50"})["events"]
    kept = [(e["kind"], e["secret"]) for e in events if e["kind"].startswith("secret.")]
    assert kept == [("secret.delete", "API_KEY"), ("secret.set", "EXTRA"), ("secret.set", "API_KEY")]
    assert VALUE not in str(events)
    assert VALUE not in "".join(p.read_text() for p in (api.root / "data" / "runs").rglob("*") if p.is_file())


@pytest.mark.parametrize(
    ("method", "path", "body", "status", "code"),
    [
        ("PUT", "/v1/programs/nothing/secrets/API_KEY", {"value": "v"}, 404, "E_PROGRAM_MISSING"),
        ("PUT", "/v1/programs/sample/secrets/API_KEY", {"value": 5}, 400, "E_BAD_REQUEST"),
        ("PUT", "/v1/programs/sample/secrets/API_KEY", {}, 400, "E_BAD_REQUEST"),
        ("PUT", "/v1/programs/sample/secrets/lower", {"value": "v"}, 400, "E_SECRET_NAME"),
        ("PUT", "/v1/programs/sample/secrets/API_KEY", {"value": ""}, 400, "E_SECRET_VALUE"),
        ("GET", "/v1/programs/nothing/secrets", None, 404, "E_PROGRAM_MISSING"),
        ("DELETE", "/v1/programs/nothing/secrets/API_KEY", None, 404, "E_PROGRAM_MISSING"),
    ],
)
async def test_refusals(api, method, path, body, status, code):
    deploy(api)
    got, answer = await send(api, method, path, **({"json": body} if body is not None else {}))
    assert (got, answer["error"]["code"]) == (status, code)


async def test_a_rrotor_with_no_vault_refuses(api):
    deploy(api)
    api.services.runner.vault = None
    status, body = await send(api, "GET", "/v1/programs/sample/secrets")
    assert (status, body["error"]["code"]) == (503, "E_VAULT")


async def test_a_follower_refuses_writes_and_asks_its_head_for_the_list(api):
    deploy(api)
    asked: list[tuple[str, dict[str, Any]]] = []

    async def forward(op: str, args: dict[str, Any], target: str | None = None) -> dict[str, Any]:
        asked.append((op, args))
        return {"result": {"program": "sample", "secrets": [{"name": "API_KEY", "declared": True, "set": True}]}}

    api.services.cluster = cast(
        Any, SimpleNamespace(role="follower", member=True, state={"head": "n0"}, forward=forward)
    )
    status, body = await send(api, "PUT", "/v1/programs/sample/secrets/API_KEY", json={"value": "v"})
    assert (status, body["error"]["code"], body["error"]["head"]) == (503, "E_NOT_HEAD", "n0")
    assert "Secrets are set on the cluster's head (n0)" in body["error"]["message"]
    status, body = await send(api, "DELETE", "/v1/programs/sample/secrets/API_KEY")
    assert (status, body["error"]["code"]) == (503, "E_NOT_HEAD")
    status, body = await send(api, "GET", "/v1/programs/sample/secrets")
    assert body["secrets"][0]["set"] is True and asked == [("secrets", {"program": "sample"})]
    assert api.services.runner.vault is not None and api.services.runner.vault.names("sample") == []


async def test_the_head_answers_a_forwarded_list(api):
    from rrotord.api import forward

    deploy(api)
    api.services.runner.vault.set("sample", "API_KEY", VALUE)
    got = await forward._serve(api.services, "secrets", {"program": "sample"})
    assert got["secrets"][0] == {"name": "API_KEY", "declared": True, "set": True}


async def call(api: Api, tool_name: str, **arguments: Any) -> dict[str, Any]:
    status, body = await rpc(api, "tools/call", {"name": tool_name, "arguments": arguments})
    assert status == 200, body
    result: dict[str, Any] = body["result"]
    return result


async def test_the_mcp_tools(api):
    deploy(api)
    done = await call(api, "secret_set", program="sample", name="API_KEY", value=VALUE)
    assert done["isError"] is False and done["structuredContent"] == {
        "program": "sample",
        "name": "API_KEY",
        "set": True,
    }
    listed = await tool(api, "secrets_list", program="sample")
    assert listed["structuredContent"]["secrets"][0] == {"name": "API_KEY", "declared": True, "set": True}
    assert VALUE not in str(listed)
    deleted = await call(api, "secret_delete", program="sample", name="API_KEY")
    assert deleted["structuredContent"]["set"] is False
    bad = await call(api, "secret_set", program="sample", name="API_KEY")
    assert bad["isError"] is True and bad["structuredContent"]["error"]["code"] == "E_BAD_REQUEST"
    nameless = await tool(api, "secrets_list")
    assert nameless["isError"] is True and nameless["structuredContent"]["error"]["message"] == "Send {program}."
