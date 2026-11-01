"""A program running a CLI (ctx.cli), end to end: real worker processes (the host's Python, no sandbox), the program
asking, the runner running the command as the CLI's own run (its HOME, its secrets, its patterns) and answering;
an approval pattern a step of the program's run that the runner itself checks; refusals audited."""

from __future__ import annotations

from typing import Any

import pytest

from rrotor.ctx import RunState, cli_step
from rrotord.audit import Audit
from rrotord.vault import Vault
from tests.daemon.runner.test_engine import Env
from tests.fakes import ECHO, FakeFetch, cli_manifest, install, install_cli, manifest, outcome, sha, tarball

URL = "https://example.com/tool.tar.gz"

PROGRAM = """
from rrotor import CliError, Result

def start(ctx, inputs):
    try:
        got = ctx.cli(inputs.get("cli", "tool"), inputs["args"], stdin=inputs.get("stdin"))
    except CliError as err:
        return Result.done({"refused": err.code})
    ctx.checkpoint("after", lambda: 1)
    return Result.done({"exit": got.exit_code, "out": got.stdout})
"""


@pytest.fixture
async def env(tmp_path, short_dir):
    e = Env(tmp_path / "data", short_dir)
    e.runner.vault = Vault(tmp_path / "data")
    e.runner.audit = Audit(tmp_path / "audit", "rrotor-1", tier=lambda: "enterprise", payloads=True)
    yield e
    await e.runner.shutdown()


async def deploy(env: Env, cli: dict[str, Any] | None = None, **program: Any) -> None:
    archive = tarball({"tool/bin/tool": ECHO}, {"tool/bin/tool": 0o755})
    block = {"install": {"url": URL, "sha256": sha(archive)}, "home": True} | (cli or {})
    await install_cli(env.programs, cli_manifest(cli=block, secrets=["TOKEN"]), FakeFetch({URL: archive}))
    Vault(env.programs.root.parent).set("tool", "TOKEN", "tok-1234567")
    data = manifest("prog", clis=["tool"], **program)
    install(env.programs, data, PROGRAM, sha="d" * 40)


async def test_a_program_runs_a_cli_as_the_clis_own_run(env):
    await deploy(env, {"env": {"TOKEN": "secret:TOKEN"}, "allow": ["pr *"]})
    run = await env.runner.call(program="prog", function="start", inputs={"args": ["pr", "list"]})
    data = outcome(run)["data"]
    assert run.status == "done" and data["exit"] == 3, run.result
    assert data["out"] == "args: pr list\ntoken: [secret]\nhome: " + str(env.programs.folder("tool") / "home") + "\n"
    events = [e for e in env.runner.audit.read(None, 20)[0] if e["kind"] == "run.call"]
    by = next(e for e in events if e.get("by") == run.id)
    assert (by["program"], by["command"], by["outcome"]["status"]) == ("tool", "pr list", "done")
    assert any(line.get("event") == "cli" for line in env.records.lines(run.id))
    step = cli_step("tool", ["pr", "list"], None)
    assert step in RunState.from_json(env.runner.get(run.id).state).checkpoints


async def test_what_the_cli_does_not_allow_is_refused_in_the_program(env):
    await deploy(env, {"allow": ["pr *"]})
    run = await env.runner.call(program="prog", function="start", inputs={"args": ["repo", "delete"]})
    assert outcome(run)["data"] == {"refused": "E_CLI_DENIED"}
    run = await env.runner.call(program="prog", function="start", inputs={"cli": "other", "args": []})
    assert outcome(run)["data"] == {"refused": "E_CLI_UNDECLARED"}


async def test_an_approval_pattern_waits_in_the_program_then_runs(env):
    await deploy(env, {"approvals": ["pr merge *"]})
    run = await env.runner.call(program="prog", function="start", inputs={"args": ["pr", "merge", "7"]})
    assert (run.status, run.result["step"]) == ("needs_approval", cli_step("tool", ["pr", "merge", "7"], None))
    assert env.runner.approvals()[0]["program"] == "prog"
    done = await env.runner.resume(run.id, {"approved": True})
    assert (done.status, outcome(done)["data"]["exit"]) == ("done", 3), done.result
    kept = RunState.from_json(env.runner.get(run.id).state)
    assert "after" in kept.checkpoints and kept.approvals  # the continue's approval and the checkpoints after it


async def test_the_runner_refuses_a_command_the_person_did_not_approve(env):
    await deploy(env, {"approvals": ["pr merge *"]})
    run = await env.runner.call(program="prog", function="start", inputs={"args": ["pr", "merge", "1"]})
    asked = {"cli": "tool", "args": ["pr", "merge", "1"], "stdin": None}
    answer = await env.runner.program_cli(run, asked)  # a program claiming its approval: the runner checks
    assert answer["data"]["code"] == "E_CLI_DECLINED"
    other = await env.runner.program_cli(run, {"cli": "nope", "args": []})
    assert other["data"]["code"] == "E_CLI_UNDECLARED"
    refused = [e for e in env.runner.audit.read(None, 20)[0] if e.get("by") == run.id]
    assert {e["outcome"]["code"] for e in refused} == {"E_CLI_DECLINED", "E_CLI_UNDECLARED"}
    await env.runner.cancel(run.id)


async def test_a_cli_removed_since_the_deploy_is_missing(env):
    await deploy(env)
    for version in env.programs.versions("tool"):
        env.programs.remove(version)
    run = await env.runner.call(program="prog", function="start", inputs={"args": []})
    assert outcome(run)["data"] == {"refused": "E_CLI_MISSING"}
    patterns = env.runner._cli_patterns("prog")  # a program, not a CLI
    assert patterns is None
