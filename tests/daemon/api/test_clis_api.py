"""CLIs over REST and MCP: listed, run, refused, approved; a program and a CLI each refuse the other's tool."""

from __future__ import annotations

from typing import Any

import pytest

from rrotord.api import ops
from rrotord.errors import BadRequest
from tests.daemon.api.conftest import Api
from tests.daemon.api.test_mcp import tool
from tests.fakes import ECHO, FakeFetch, cli_manifest, install, install_cli, manifest, sha, tarball

URL = "https://example.com/tool.tar.gz"


async def send(api: Api, method: str, path: str, **kw: Any) -> tuple[int, Any]:
    res = await api.client.request(method, path, headers=api.auth(), **kw)
    return res.status, await res.json()


async def deploy(api: Api, **cli: Any) -> None:
    archive = tarball({"bin/tool": ECHO}, {"bin/tool": 0o755})
    block = {"install": {"url": URL, "sha256": sha(archive)}, **cli}
    await install_cli(api.services.programs, cli_manifest(cli=block), FakeFetch({URL: archive}))


async def test_clis_are_listed_and_run_over_rest(api):
    await deploy(api, allow=["pr *"], approvals=["pr merge *"])
    install(api.services.programs, manifest("sample"), "def start(ctx, inputs):\n    pass\n", sha="d" * 40)
    status, body = await send(api, "GET", "/v1/clis")
    assert status == 200 and [c["name"] for c in body["clis"]] == ["tool"]
    listed = body["clis"][0]
    assert (listed["command"], listed["install"], listed["needs_approval"]) == (
        "tool",
        {"source": "url", "pinned": URL},
        True,
    )
    status, run = await send(api, "POST", "/v1/clis/tool/run", json={"args": ["pr", "list"]})
    assert (status, run["status"], run["function"], run["data"]["exit_code"]) == (200, "done", "cli", 3)
    status, refused = await send(api, "POST", "/v1/clis/tool/run", json={"args": ["repo", "delete"]})
    assert (status, refused["error"]["code"]) == (403, "E_CLI_DENIED")
    status, waiting = await send(api, "POST", "/v1/clis/tool/run", json={"args": ["pr", "merge", "1"]})
    assert (status, waiting["status"], waiting["step"]) == (200, "needs_approval", "cli")
    path = f"/v1/runs/{waiting['run_id']}/continue"
    status, done = await send(api, "POST", path, json={"answer": {"approved": True}})
    assert (status, done["status"]) == (200, "done")
    status, logged = await send(api, "GET", f"/v1/runs/{waiting['run_id']}/log")
    assert status == 200 and any(line.get("command") == "pr merge 1" for line in logged["lines"])


async def test_clis_over_mcp_and_each_kind_refuses_the_others_tool(api):
    await deploy(api)
    install(api.services.programs, manifest("sample"), "def start(ctx, inputs):\n    pass\n", sha="d" * 40)
    listed = await tool(api, "clis_list")
    assert listed["structuredContent"]["clis"][0]["name"] == "tool"
    ran = await tool(api, "cli_run", cli="tool", args=["x"], stdin="in", files={"a.txt": "hi"})
    assert ran["isError"] is False and ran["structuredContent"]["data"]["stdout"].startswith("args: x\n")
    wrong = await tool(api, "cli_run", cli="sample", args=[])
    assert wrong["isError"] and wrong["structuredContent"]["error"]["code"] == "E_NOT_CLI"
    other = await tool(api, "program_call", program="tool")
    assert other["isError"] and other["structuredContent"]["error"]["code"] == "E_NOT_PROGRAM"


async def test_a_cli_run_without_a_cli_is_refused(api):
    with pytest.raises(BadRequest, match=r"Send \{cli, args"):
        await ops.cli_run(api.services, {"args": []})
    with pytest.raises(BadRequest, match=r"Send \{cli\}"):
        await ops.cli_login(api.services, {})


SIGN_IN = '#!/bin/sh\necho "visit https://auth.example.com/x"\nread code\necho "$code" > "$HOME/creds"\n'


async def deploy_sign_in(api: Api) -> None:
    archive = tarball({"bin/tool": SIGN_IN}, {"bin/tool": 0o755})
    block = {"install": {"url": URL, "sha256": sha(archive)}, "home": True, "login": {"args": ["login"]}}
    await install_cli(api.services.programs, cli_manifest(cli=block), FakeFetch({URL: archive}))


async def test_a_cli_signs_in_over_rest(api):
    await deploy_sign_in(api)
    status, listed = await send(api, "GET", "/v1/clis")
    assert listed["clis"][0]["login"] == {"args": ["login"]} and listed["clis"][0]["builtin"] is None
    status, asked = await send(api, "POST", "/v1/clis/tool/login")
    assert (status, asked["status"], asked["data"]["url"]) == (200, "needs_input", "https://auth.example.com/x")
    status, done = await send(api, "POST", f"/v1/runs/{asked['run_id']}/continue", json={"answer": "code-123"})
    assert (status, done["status"]) == (200, "done")
    assert (api.services.programs.folder("tool") / "home" / "creds").read_text() == "code-123\n"


async def test_a_cli_signs_in_over_mcp(api):
    await deploy_sign_in(api)
    asked = await tool(api, "cli_login", cli="tool")
    assert asked["isError"] is False and asked["structuredContent"]["status"] == "needs_input"
    done = await tool(api, "program_continue", run_id=asked["structuredContent"]["run_id"], answer="c-1")
    assert done["structuredContent"]["status"] == "done"
    missing = await tool(api, "cli_login", cli="nothing")
    assert missing["isError"] and missing["structuredContent"]["error"]["code"] == "E_PROGRAM_MISSING"
