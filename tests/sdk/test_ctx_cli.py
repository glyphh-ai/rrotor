"""ctx.cli: a program runs a command of a CLI on its rrotor, checked against the CLI's patterns first, asked approval
for as a step of its own run, answered once per run."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rrotor.ctx import CliResult, Ctx, RunState, cli_step
from rrotor.errors import CliError, Wait
from rrotor.http import Http

DONE = {"status": "done", "data": {"exit_code": 0, "stdout": "sent\n", "stderr": "", "truncated": False, "files": {}}}


def make(
    tmp_path: Path, clis: dict[str, Any], answer: dict[str, Any] = DONE, state: RunState | None = None
) -> tuple[Ctx, list[dict[str, Any]]]:
    asked: list[dict[str, Any]] = []
    sunk: list[tuple[str, dict[str, Any]]] = []

    def request(ask: dict[str, Any]) -> dict[str, Any]:
        asked.append(ask)
        return answer

    ctx = Ctx(
        inputs={},
        approvals=frozenset(),
        state=state or RunState(),
        files=tmp_path,
        http=Http(frozenset(), None),
        sink=lambda op, data: sunk.append((op, data)),
        clis=clis,
        request=request,
    )
    return ctx, asked


def test_a_command_runs_once_per_run(tmp_path):
    ctx, asked = make(tmp_path, {"claude": {"allow": ["-p *"], "approvals": []}})
    got = ctx.cli("claude", ["-p", "email it"])
    assert got == CliResult(0, "sent\n", "") and got.ok
    step = cli_step("claude", ["-p", "email it"], None)
    assert asked == [{"cli": "claude", "args": ["-p", "email it"], "stdin": None, "step": step, "env": {}}]
    assert ctx.cli("claude", ["-p", "email it"]) == got and len(asked) == 1  # a resumed run reads it back
    assert step.startswith("cli:claude:") and step != cli_step("claude", ["-p", "other"], None)


def test_what_a_cli_refuses_never_reaches_rrotor(tmp_path):
    ctx, asked = make(tmp_path, {"claude": {"allow": ["-p *"]}, "gone": None})
    with pytest.raises(CliError) as undeclared:
        ctx.cli("codex", ["exec", "x"])
    with pytest.raises(CliError) as missing:
        ctx.cli("gone", [])
    with pytest.raises(CliError) as denied:
        ctx.cli("claude", ["auth", "logout"])
    codes = (undeclared.value.code, missing.value.code, denied.value.code)
    assert codes == ("E_CLI_UNDECLARED", "E_CLI_MISSING", "E_CLI_DENIED") and asked == []
    assert '"clis": ["codex"]' in undeclared.value.message


def test_an_approval_pattern_is_a_step_of_the_run(tmp_path):
    clis = {"gh": {"allow": [], "approvals": ["pr merge *"]}}
    ctx, asked = make(tmp_path, clis)
    with pytest.raises(Wait) as waiting:
        ctx.cli("gh", ["pr", "merge", "7"])
    step = cli_step("gh", ["pr", "merge", "7"], None)
    assert waiting.value.pending == {"kind": "approval", "key": step}
    assert "pr merge *" in str(waiting.value.result.prompt) and asked == []  # type: ignore[attr-defined]
    approved, asked = make(tmp_path, clis, state=RunState(approvals={step: True}))
    assert approved.cli("gh", ["pr", "merge", "7"]).ok and len(asked) == 1
    declined, asked = make(tmp_path, clis, state=RunState(approvals={step: False}))
    with pytest.raises(CliError) as caught:
        declined.cli("gh", ["pr", "merge", "7"])
    assert caught.value.code == "E_CLI_DECLINED" and asked == []


def test_a_command_that_did_not_run_fails_with_its_code(tmp_path):
    failed = {"status": "failed", "data": {"code": "E_SECRET_MISSING", "reason": "no key"}}
    ctx, _ = make(tmp_path, {"claude": {}}, answer=failed)
    with pytest.raises(CliError, match="no key") as caught:
        ctx.cli("claude", ["-p", "x"], stdin="in")
    assert caught.value.code == "E_SECRET_MISSING"
    ctx, _ = make(tmp_path, {"claude": {}}, answer={"status": "needs_approval"})
    with pytest.raises(CliError, match="needs_approval") as odd:
        ctx.cli("claude", ["-p", "x"])
    assert odd.value.code == "E_CLI"


def test_without_rrotor_no_cli_runs(tmp_path):
    ctx = Ctx(inputs={}, approvals=frozenset(), state=RunState(), files=tmp_path, http=Http(frozenset(), None),
              clis={"claude": {}})  # fmt: skip
    with pytest.raises(CliError) as caught:
        ctx.cli("claude", ["-p", "x"])
    assert caught.value.code == "E_CLI_MISSING"


def test_env_goes_with_the_command_once_checked(tmp_path):
    ctx, asked = make(tmp_path, {"gh": {"allow": []}})
    assert ctx.cli("gh", ["pr", "list"], env={"GH_TOKEN": "ghp_123", "GH_HOST": "github.com"}).ok
    assert asked[0]["env"] == {"GH_TOKEN": "ghp_123", "GH_HOST": "github.com"}
    for bad in ({"gh_token": "x"}, {"GH TOKEN": "x"}, {"GH_TOKEN": 1}, {f"K{i}": "v" for i in range(33)}, "x=1"):
        with pytest.raises(CliError) as caught:
            ctx.cli("gh", ["pr", "view"], env=bad)  # type: ignore[arg-type]
        assert caught.value.code == "E_CLI_ENV" and len(asked) == 1, bad


def test_a_cli_result_round_trips():
    got = CliResult(2, "o", "e", True)
    assert CliResult.from_json(got.to_json()) == got and not got.ok
