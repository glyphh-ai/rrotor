"""CLIs over REST and MCP: listed, run, refused, approved; a program and a CLI each refuse the other's tool."""

from __future__ import annotations

from typing import Any

import pytest

from rrotord.api import ops
from rrotord.audit import Who, acting
from rrotord.errors import BadRequest, Forbidden
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
    with pytest.raises(BadRequest, match=r"Send \{cli, shared\?\}"):
        await ops.cli_login(api.services, {})
    with pytest.raises(BadRequest, match=r"Send \{cli, shared\?\}"):
        await ops.cli_login(api.services, {"cli": "tool", "shared": "yes"})


SIGN_IN = '#!/bin/sh\necho "visit https://auth.example.com/x"\nread code\necho "$code" > "$HOME/creds"\n'


async def deploy_sign_in(api: Api) -> None:
    archive = tarball({"bin/tool": SIGN_IN}, {"bin/tool": 0o755})
    login = {"args": ["login"], "signed_in": "creds"}
    block = {"install": {"url": URL, "sha256": sha(archive)}, "home": True, "login": login}
    await install_cli(api.services.programs, cli_manifest(cli=block), FakeFetch({URL: archive}))


async def test_a_cli_signs_in_over_rest(api):
    await deploy_sign_in(api)
    status, listed = await send(api, "GET", "/v1/clis")
    assert listed["clis"][0]["login"] == {"args": ["login"]} and listed["clis"][0]["builtin"] is None
    assert listed["clis"][0]["signed_in"] is False
    status, asked = await send(api, "POST", "/v1/clis/tool/login")
    assert (status, asked["status"], asked["data"]["url"]) == (200, "needs_input", "https://auth.example.com/x")
    status, done = await send(api, "POST", f"/v1/runs/{asked['run_id']}/continue", json={"answer": "code-123"})
    assert (status, done["status"]) == (200, "done")
    assert (api.services.programs.folder("tool") / "home" / "creds").read_text() == "code-123\n"
    status, listed = await send(api, "GET", "/v1/clis")
    assert listed["clis"][0]["signed_in"] is True  # for this caller (the local token: the CLI's own HOME)
    with acting(Who({"type": "sign-in", "sub": "user_9"})):  # another person: their own HOME, not signed in
        assert (await ops.clis_list(api.services))["clis"][0]["signed_in"] is False


async def test_a_signed_in_person_signs_a_cli_in_as_themselves(api):
    """The HOME a sign-in lands in is the caller's: a Glyphh sign-in's subject gets homes/<subject>."""
    await deploy_sign_in(api)
    with acting(Who({"type": "sign-in", "sub": "user_1"})):
        asked = await ops.cli_login(api.services, {"cli": "tool"})
        done = await ops.resume(api.services, asked["run_id"], "code-1")
        assert done["status"] == "done" and (await ops.clis_list(api.services))["clis"][0]["signed_in"] is True
    assert (api.services.programs.folder("tool") / "homes" / "user_1" / "creds").read_text() == "code-1\n"
    assert not (api.services.programs.folder("tool") / "home").exists()
    assert (await ops.clis_list(api.services))["clis"][0]["signed_in"] is False  # the local token's HOME


async def test_the_shared_sign_in_is_an_admins_act_or_the_local_tokens(api):
    """`shared: true` signs the CLI's own HOME in (what an app of run_as app, a schedule or a key's call uses): the
    local token may, an admin may, a member or a key is refused; clis_list says shared_signed_in."""
    await deploy_sign_in(api)
    status, listed = await send(api, "GET", "/v1/clis")
    assert (listed["clis"][0]["signed_in"], listed["clis"][0]["shared_signed_in"]) == (False, False)
    for who in (
        Who({"type": "sign-in", "sub": "user_2", "role": "member"}),
        Who({"type": "sign-in", "sub": "user_3"}),
        Who({"type": "key", "key_sha256": "a" * 64}),
    ):
        with acting(who), pytest.raises(Forbidden) as refused:
            await ops.cli_login(api.services, {"cli": "tool", "shared": True})
        assert refused.value.code == "E_FORBIDDEN"
    assert not (api.services.programs.folder("tool") / "home").exists()
    with acting(Who({"type": "sign-in", "sub": "user_1", "role": "admin"})):  # an admin: the shared HOME
        asked = await ops.cli_login(api.services, {"cli": "tool", "shared": True})
        done = await ops.resume(api.services, asked["run_id"], "code-shared")
        assert done["status"] == "done"
        mine = (await ops.clis_list(api.services))["clis"][0]
        assert (mine["signed_in"], mine["shared_signed_in"]) == (False, True)  # theirs is not, the shared one is
    assert (api.services.programs.folder("tool") / "home" / "creds").read_text() == "code-shared\n"
    run = api.services.runner.get(asked["run_id"])
    assert run.subject is None
    status, asked = await send(api, "POST", "/v1/clis/tool/login", json={"shared": True})  # the local token may
    assert (status, asked["status"]) == (200, "needs_input")
    await send(api, "POST", f"/v1/runs/{asked['run_id']}/continue", json={"answer": "code-local"})
    assert (api.services.programs.folder("tool") / "home" / "creds").read_text() == "code-local\n"
    shared = await tool(api, "cli_login", cli="tool", shared=True)
    assert shared["isError"] is False and shared["structuredContent"]["status"] == "needs_input"
    await tool(api, "program_cancel", run_id=shared["structuredContent"]["run_id"])


async def test_a_cli_signs_in_over_mcp(api):
    await deploy_sign_in(api)
    asked = await tool(api, "cli_login", cli="tool")
    assert asked["isError"] is False and asked["structuredContent"]["status"] == "needs_input"
    done = await tool(api, "program_continue", run_id=asked["structuredContent"]["run_id"], answer="c-1")
    assert done["structuredContent"]["status"] == "done"
    missing = await tool(api, "cli_login", cli="nothing")
    assert missing["isError"] and missing["structuredContent"]["error"]["code"] == "E_PROGRAM_MISSING"
