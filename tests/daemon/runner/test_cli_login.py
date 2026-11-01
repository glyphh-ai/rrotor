"""A CLI's sign-in from afar, end to end: real worker processes (the host's Python, no sandbox) running sign-in
scripts on a pseudo-terminal, as the vendors' CLIs behave: an address and a pasted code, a device code, a polled
flow, one that never exits once signed in; the command kept running across the wait, the code typed into it and
never kept, the sign-in left in the private HOME; cancelled or refused cleanly."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from rrotord.audit import Audit
from rrotord.errors import BadRequest
from rrotord.vault import Vault
from tests.daemon.runner.test_engine import Env
from tests.fakes import FakeFetch, cli_manifest, install_cli, outcome, sha, tarball

URL = "https://example.com/tool.tar.gz"
CODE = "Xk29-the-code#state-1234"

PASTE = """#!/bin/sh
echo $$ > "$HOME/pid"
echo "Opening browser to sign in..."
printf 'If the browser did not open, visit: \\033]8;;https://auth.example.com/oauth?state=abc\\007https://auth.example.com/oauth?state=abc\\033]8;;\\007\\r\\n'
printf 'Paste code here if prompted > '
read code
echo "$code" > "$HOME/creds"
echo "Login successful. key=${KEY-unset}"
"""

DEVICE = """#!/bin/sh
echo "To authenticate, visit https://github.example.com/login/device and enter code FA16-5343"
echo "Waiting for authorization..."
sleep 2
echo ok > "$HOME/auth"
echo "Signed in."
"""

POLLED = """#!/bin/sh
echo "Open a browser and navigate to this link: https://cursor.example.com/login?challenge=c1"
sleep 3
echo "Logged in."
"""

STAYS = """#!/bin/sh
echo "Please visit the following URL to authorize the application:"
echo "https://accounts.example.com/o/oauth2/v2/auth?client_id=1"
printf 'Enter the authorization code: '
read code
mkdir -p "$HOME/.g" && echo "$code" > "$HOME/.g/creds"
echo "chat ready"
sleep 300
"""


@pytest.fixture
async def env(tmp_path, short_dir):
    e = Env(tmp_path / "data", short_dir)
    e.runner.vault = Vault(tmp_path / "data")
    e.runner.audit = Audit(tmp_path / "audit", "rrotor-1", tier=lambda: "enterprise", payloads=True)
    yield e
    await e.runner.shutdown()


async def deploy(env: Env, script: str, login: dict[str, Any] | None = None, **extra: Any) -> None:
    archive = tarball({"tool/bin/tool": script}, {"tool/bin/tool": 0o755})
    block: dict[str, Any] = {"install": {"url": URL, "sha256": sha(archive)}, "home": True}
    block |= {"login": login} if login is not None else {}
    block |= extra.pop("cli", {})
    await install_cli(env.programs, cli_manifest(cli=block, **extra), FakeFetch({URL: archive}))


def home(env: Env) -> Any:
    return env.programs.folder("tool") / "home"


async def test_an_address_and_a_pasted_code_sign_the_cli_in(env):
    await deploy(env, PASTE, {"args": ["auth", "login"], "signed_in": "creds"})
    run = await env.runner.cli_login(cli="tool")
    data = outcome(run)["data"]
    assert (run.status, run.result["step"], run.inputs["login"]) == ("needs_input", "login", True)
    assert data["url"] == "https://auth.example.com/oauth?state=abc" and data["code"] is None
    assert "Paste code here if prompted >" in data["output"] and "\x1b" not in data["output"]
    assert "the code the page shows" in run.result["prompt"] and run.pending == {"kind": "login", "key": "cli"}
    done = await env.runner.resume(run.id, CODE)
    data = outcome(done)["data"]
    assert (done.status, data["exit_code"], data["signed_in"]) == ("done", 0, True), data
    assert "Login successful. key=unset" in data["output"]
    assert (home(env) / "creds").read_text() == CODE + "\n"  # the sign-in kept in the private HOME
    assert CODE not in data["output"] and "[answer]" in data["output"]  # the terminal's echo, masked
    folder = env.records.folder(run.id)
    assert all(CODE not in path.read_text() for path in folder.iterdir() if path.is_file())
    call = next(e for e in env.runner.audit.read(None, 10)[0] if e["kind"] == "run.call")
    assert call["command"] == "auth login"


async def test_a_device_code_is_shown_and_any_answer_waits_for_the_cli(env):
    await deploy(env, DEVICE, {"args": ["login", "--device"]})
    run = await env.runner.cli_login(cli="tool")
    data = outcome(run)["data"]
    assert (data["url"], data["code"]) == ("https://github.example.com/login/device", "FA16-5343")
    assert "enter the code FA16-5343" in run.result["prompt"]
    done = await env.runner.resume(run.id, {"code": "done"})  # not typed: the CLI finds out itself
    assert (done.status, outcome(done)["data"]["exit_code"], outcome(done)["data"]["signed_in"]) == ("done", 0, None)
    assert (home(env) / "auth").is_file()


async def test_a_polled_sign_in_asks_for_no_code(env):
    await deploy(env, POLLED, {"args": ["login"], "paste": False})
    run = await env.runner.cli_login(cli="tool")
    assert outcome(run)["data"]["url"] == "https://cursor.example.com/login?challenge=c1"
    assert "continue this run (any answer" in run.result["prompt"]
    done = await env.runner.resume(run.id, None)
    assert "Logged in." in outcome(done)["data"]["output"]


async def test_a_cli_that_stays_up_once_signed_in_is_stopped(env):
    settings = '{"auth": "oauth"}\n'
    login = {"args": [], "signed_in": ".g/creds", "home_files": {".g/settings.json": settings}}
    await deploy(env, STAYS, login)
    run = await env.runner.cli_login(cli="tool")
    assert run.status == "needs_input" and (home(env) / ".g" / "settings.json").read_text() == settings
    done = await env.runner.resume(run.id, "4/0AbCdEf")
    data = outcome(done)["data"]
    assert (done.status, data["exit_code"], data["signed_in"]) == ("done", None, True)
    assert (home(env) / ".g" / "creds").read_text() == "4/0AbCdEf\n"


async def test_cli_run_of_its_sign_in_command_is_its_sign_in(env):
    await deploy(env, PASTE, {"args": ["auth", "login", "--claudeai"]}, cli={"allow": ["-p *"]})
    run = await env.runner.cli_run(cli="tool", args=["auth", "login"])
    assert (run.status, run.inputs["login"]) == ("needs_input", True)
    assert run.inputs["args"] == ["auth", "login", "--claudeai"]
    await env.runner.cancel(run.id)


async def test_a_cancelled_sign_in_stops_its_command(env):
    await deploy(env, PASTE, {"args": ["login"]})
    run = await env.runner.cli_login(cli="tool")
    pid = int((home(env) / "pid").read_text())
    cancelled = await env.runner.cancel(run.id)
    assert cancelled.status == "cancelled"
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("the sign-in's command is still running")


async def test_an_optional_secret_is_passed_only_when_set(env):
    await deploy(env, PASTE, {"args": ["login"]}, cli={"env": {"KEY": "secret?:KEY"}}, secrets=["KEY"])
    env.runner.vault.set("tool", "KEY", "sk-optional-1234")
    run = await env.runner.cli_login(cli="tool")
    done = await env.runner.resume(run.id, "code-1")
    assert "key=[secret]" in outcome(done)["data"]["output"]


async def test_a_cli_without_a_login_is_refused_and_audited(env):
    await deploy(env, PASTE, None)
    with pytest.raises(BadRequest) as caught:
        await env.runner.cli_login(cli="tool")
    assert caught.value.code == "E_CLI_LOGIN"
    event = env.runner.audit.read(None, 10)[0][0]
    assert event["outcome"] == {"status": "refused", "code": "E_CLI_LOGIN"} and event["command"] is None


async def test_a_sign_in_whose_command_cannot_start_fails(env):
    await deploy(env, PASTE, {"args": ["login"]})
    (env.programs.get("tool").root / "cli" / "files" / "tool" / "bin" / "tool").unlink()
    run = await env.runner.cli_login(cli="tool")
    assert (run.status, outcome(run)["data"]["code"]) == ("failed", "E_CLI_EXEC")
