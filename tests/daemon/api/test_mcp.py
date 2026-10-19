"""The runtime's MCP server: initialize and discover, the tools listed, each tool against the real runner, refusals
as tool errors, and the JSON-RPC rules."""

from __future__ import annotations

import json
from typing import Any

from rrotord.api import mcp
from tests.daemon.api.conftest import Api, linked
from tests.fakes import DONE_MODEL, GitRepo, install, manifest, token

WAITS = "from rrotor import Result\n\ndef start(ctx, inputs):\n    return Result.needs_input('day?')\n"
META = {"_meta": {mcp.VERSION_META: mcp.MODERN}}


async def rpc(api: Api, method: str, params: dict[str, Any] | None = None, rpc_id: Any = 1) -> tuple[int, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if rpc_id is not None:
        body["id"] = rpc_id
    res = await api.client.post("/mcp", json=body, headers=api.auth())
    return res.status, (await res.json() if res.content_type == "application/json" else None)


async def tool(api: Api, name: str, **arguments: Any) -> dict[str, Any]:
    status, body = await rpc(api, "tools/call", {"name": name, "arguments": arguments})
    assert status == 200, body
    result: dict[str, Any] = body["result"]
    return result


async def test_initialize_discover_ping_and_the_tools_listed(api):
    status, body = await rpc(api, "initialize", {"protocolVersion": "2025-06-18"})
    result = body["result"]
    assert status == 200 and result["protocolVersion"] == "2025-06-18" and result["serverInfo"]["name"] == "rrotor"
    assert (await rpc(api, "initialize", {"protocolVersion": "1999-01-01"}))[1]["result"][
        "protocolVersion"
    ] == "2025-11-25"
    status, body = await rpc(api, "server/discover", META)
    assert body["result"]["supportedVersions"][0] == mcp.MODERN and body["result"]["resultType"] == "complete"
    assert body["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "rrotor"
    status, body = await rpc(api, "initialize", {"protocolVersion": "2025-06-18", **META})
    assert status == 404 and body["error"]["code"] == mcp.METHOD_NOT_FOUND and body["id"] == 1
    assert (await rpc(api, "ping"))[1]["result"] == {}
    tools = (await rpc(api, "tools/list"))[1]["result"]["tools"]
    assert [t["name"] for t in tools] == [
        "models_list",
        "model_call",
        "model_continue",
        "model_cancel",
        "run_status",
        "run_log",
        "approvals",
        "model_deploy",
    ]
    assert tools[0]["annotations"]["readOnlyHint"] is True and tools[1]["inputSchema"]["required"] == ["model"]


async def test_the_tools_run_models_and_read_runs(api):
    install(api.services.models, manifest("sample"), DONE_MODEL)
    install(api.services.models, manifest("asks"), WAITS, sha="b" * 40)
    listed = await tool(api, "models_list")
    assert [m["name"] for m in listed["structuredContent"]["models"]] == ["asks", "sample"]
    assert json.loads(listed["content"][0]["text"]) == listed["structuredContent"]
    done = await tool(api, "model_call", model="sample", inputs={"x": 1})
    assert done["isError"] is False and done["structuredContent"]["status"] == "done"
    assert done["structuredContent"]["data"] == {"echo": {"x": 1}}
    waiting = (await tool(api, "model_call", model="asks"))["structuredContent"]
    assert waiting["status"] == "needs_input" and waiting["prompt"] == "day?"
    status = await tool(api, "run_status", run_id=waiting["run_id"])
    assert status["structuredContent"]["status"] == "needs_input"
    log = await tool(api, "run_log", run_id=waiting["run_id"])
    assert isinstance(log["structuredContent"]["lines"], list)
    assert (await tool(api, "approvals"))["structuredContent"] == {"approvals": []}
    cancelled = await tool(api, "model_cancel", run_id=waiting["run_id"])
    assert cancelled["structuredContent"]["status"] == "cancelled"
    again = (await tool(api, "model_call", model="asks"))["structuredContent"]
    resumed = await tool(api, "model_continue", run_id=again["run_id"], answer="monday")
    assert resumed["structuredContent"]["run_id"] == again["run_id"]  # the model asks again: the next Result
    assert resumed["structuredContent"]["status"] == "needs_input"


async def test_a_record_refused_by_the_cap_is_model_calls_result_not_a_tool_error(api):
    from tests.daemon.runner.test_engine import FULL

    install(api.services.models, manifest("full"), FULL)
    full = await tool(api, "model_call", model="full")
    assert full["isError"] is False and full["structuredContent"]["status"] == "done"
    assert full["structuredContent"]["data"]["status"] == "limit_exceeded"
    assert full["structuredContent"]["data"]["indexed"] is False
    status, body = await rpc(api, "tools/call", {"name": "model_call", "arguments": {"model": "full"}})
    assert status == 200 and "error" not in body
    res = await api.client.post("/v1/runs", json={"model": "full"}, headers=api.auth())
    assert res.status == 200 and (await res.json())["data"]["status"] == "limit_exceeded"  # REST: 200, as data


async def test_refusals_are_tool_errors_and_bad_rpc_is_refused(api):
    missing = await tool(api, "model_call", model="nothing")
    assert missing["isError"] is True and missing["structuredContent"]["error"]["code"] == "E_MODEL_MISSING"
    assert missing["content"][0]["text"] == missing["structuredContent"]["error"]["message"]
    bad = await tool(api, "run_status")
    assert bad["isError"] is True and bad["structuredContent"]["error"]["code"] == "E_BAD_REQUEST"
    deploy = await tool(api, "model_deploy", repo="octo/x", commit="not-a-sha")
    assert deploy["isError"] is True
    status, body = await rpc(api, "tools/call", {"name": "nothing"})
    assert status == 200 and body["error"]["code"] == mcp.INVALID_PARAMS
    status, body = await rpc(api, "tools/call", {"name": "models_list", "arguments": "x"})
    assert body["error"]["code"] == mcp.INVALID_PARAMS
    status, body = await rpc(api, "resources/list")
    assert status == 200 and body["error"]["code"] == mcp.METHOD_NOT_FOUND
    status, body = await rpc(api, "resources/list", META)
    assert status == 404
    status, body = await rpc(api, "notifications/initialized", rpc_id=None)
    assert status == 202 and body is None
    res = await api.client.post("/mcp", data=b"not json", headers=api.auth())
    assert res.status == 400 and (await res.json())["error"]["code"] == mcp.PARSE_ERROR
    for body in (
        {"jsonrpc": "1.0", "method": "ping"},
        {"jsonrpc": "2.0", "method": "ping", "params": 5},
        {"jsonrpc": "2.0", "method": "ping", "id": True},
    ):
        res = await api.client.post("/mcp", json=body, headers=api.auth())
        assert res.status == 400 and (await res.json())["error"]["code"] == mcp.INVALID_REQUEST, body
    assert mcp.failure(mcp.RpcError(1, "x"))["jsonrpc"] == "2.0" and "id" not in mcp.failure(mcp.RpcError(1, "x"))


async def test_model_deploy_deploys_a_repo_commit_and_reports_it_up_the_link(api, tmp_path):
    repo = GitRepo(tmp_path / "src")
    sha = repo.model(manifest("sample"))
    repo.bare(tmp_path / "github", "owner/sample")
    link = linked(api)
    done = await tool(api, "model_deploy", repo="owner/sample", commit=sha, token=token())
    assert done.get("isError") is not True and done["structuredContent"]["commit_sha"] == sha
    [sent] = link.sent
    assert (sent["type"], sent["model"], sent["version"], sent["commit_sha"], sent["repo"]) == (
        "deployed",
        "sample",
        1,
        sha,
        "owner/sample",
    )
    assert sent["manifest"] == api.services.models.active("sample").manifest.model_dump(mode="json")
