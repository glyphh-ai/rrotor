"""The Cloud's messages against a real runner and deployer; what goes up the link is kept by a fake link."""

from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

import rrotor
from rrotord.deploy import Deployer, Tools
from rrotord.link import protocol as p
from rrotord.link.handler import LinkHandler
from rrotord.programs import Programs
from rrotord.proxy import EgressProxy
from rrotord.runner.engine import Runner
from rrotord.runner.record import Records
from tests.fakes import DONE_PROGRAM, FakeLink, GitRepo, LocalLauncher, install, manifest, ready, token

WAITS = "from rrotor import Result\n\ndef start(ctx, inputs):\n    return Result.needs_input('day?')\n"


class Env:
    def __init__(self, root: Path, sock: Path) -> None:
        self.programs = Programs(root / "data")
        self.deployer = Deployer(
            self.programs,
            root / "scratch",
            Tools(python=sys.executable, uv=shutil.which("uv") or "uv", github=f"file://{root / 'github'}"),
        )
        self.handler = LinkHandler(self.programs, cast(Runner, None), self.deployer)
        self.runner = Runner(
            programs=self.programs,
            records=Records(root / "data"),
            proxy=EgressProxy(sock),
            launcher=LocalLauncher(),
            sdk=Path(rrotor.__file__).parent,
            layers=ready(),
            notify=self.handler.notify,
        )
        self.handler.runner = self.runner
        self.link = FakeLink()
        self.handler.link = cast(Any, self.link)

    def results(self) -> list[dict[str, Any]]:
        return [m for m in self.link.sent if m["type"] == "result"]


@pytest.fixture
async def env(tmp_path, short_dir):
    e = Env(tmp_path, short_dir)
    yield e
    await e.runner.shutdown()


def call(program: str = "sample", function: str = "start", run_id: str | None = None) -> p.Call:
    return p.Call(
        type="call",
        run_id=uuid.UUID(run_id or str(uuid.uuid4())),
        program=program,
        version=1,
        function=function,
        inputs={},
        caller="routine",
    )


async def test_deployed_is_the_active_versions(env):
    install(env.programs, manifest("sample"), DONE_PROGRAM)
    assert env.handler.deployed() == [{"program": "sample", "version": 1, "commit_sha": "a" * 40}]


async def test_a_call_answers_its_result_up_the_link(env):
    install(env.programs, manifest("sample"), DONE_PROGRAM)
    message = call()
    await env.handler.on_call(message)
    result = env.results()[0]
    assert (result["run_id"], result["result"]["status"], result["seq"]) == (str(message.run_id), "done", 1)


async def test_a_call_that_is_refused_answers_failed(env):
    message = call(program="missing")
    await env.handler.on_call(message)
    assert env.results()[0]["result"]["data"]["code"] == "E_PROGRAM_MISSING"


async def test_continue_and_cancel_of_a_waiting_run(env):
    install(env.programs, manifest("sample"), WAITS)
    message = call()
    await env.handler.on_call(message)
    await env.handler.on_continue(p.Continue(type="continue", run_id=message.run_id, answer="x"))
    await env.handler.on_cancel(p.Cancel(type="cancel", run_id=message.run_id))
    assert [r["result"]["status"] for r in env.results()] == ["needs_input", "needs_input", "cancelled"]


async def test_continue_of_a_run_not_here(env):
    await env.handler.on_continue(p.Continue(type="continue", run_id=uuid.uuid4(), answer=1))
    assert env.results()[0]["result"]["data"]["code"] == "E_RUN_MISSING"


async def test_continue_of_a_run_that_ended_is_only_logged(env, caplog):
    install(env.programs, manifest("sample"), DONE_PROGRAM)
    message = call()
    await env.handler.on_call(message)
    await env.handler.on_continue(p.Continue(type="continue", run_id=message.run_id, answer=1))
    assert len(env.results()) == 1 and "continue refused" in caplog.text


async def test_cancel_of_a_run_not_here_answers_cancelled(env):
    await env.handler.on_cancel(p.Cancel(type="cancel", run_id=uuid.uuid4()))
    assert env.results()[0]["result"] == {"status": "cancelled", "data": None, "step": "cancel", "prompt": None}


async def test_cancel_of_an_ended_run_answers_nothing(env):
    install(env.programs, manifest("sample"), DONE_PROGRAM)
    message = call()
    await env.handler.on_call(message)
    await env.handler.on_cancel(p.Cancel(type="cancel", run_id=message.run_id))
    assert len(env.results()) == 1


async def test_local_runs_and_a_handler_without_a_link_send_nothing(env):
    install(env.programs, manifest("sample"), DONE_PROGRAM)
    await env.runner.call(program="sample", function="start", inputs={})
    env.handler.link = None
    await env.handler.on_call(call())
    await env.handler.on_call(call(program="missing"))
    assert env.link.sent == []


def deploy(kind: str, sha: str, program: str = "sample", repo: str | None = None) -> p.Deploy:
    return p.Deploy(type=kind, program=program, version=4, commit_sha=sha, repo=repo, token=token() if repo else None)


async def test_deploy_from_the_repo_answers_deployed(env, tmp_path):
    repo = GitRepo(tmp_path / "src")
    sha = repo.program(manifest("sample"))
    repo.bare(tmp_path / "github", "owner/sample")
    await env.handler.on_deploy(deploy("deploy", sha, repo="owner/sample"))
    assert env.link.sent == [
        {"type": "deployed", "id": env.link.sent[0]["id"], "program": "sample", "version": 4, "commit_sha": sha}
    ]
    assert env.programs.active("sample").version == 4


async def test_deploy_of_a_commit_naming_another_program_is_refused(env, tmp_path):
    repo = GitRepo(tmp_path / "src")
    sha = repo.program(manifest("other"))
    repo.bare(tmp_path / "github", "owner/sample")
    await env.handler.on_deploy(deploy("deploy", sha, repo="owner/sample"))
    event = env.link.sent[0]
    assert (event["type"], event["kind"], event["data"]["code"]) == ("event", "deploy_failed", "E_MANIFEST")
    assert env.programs.versions("other") == []


async def test_activate_a_held_commit(env):
    install(env.programs, manifest("sample"), DONE_PROGRAM, sha="c" * 40)
    await env.handler.on_deploy(deploy("activate", "c" * 40))
    assert env.link.sent[0]["type"] == "deployed" and env.link.sent[0]["version"] == 4


async def test_activate_a_commit_not_held_answers_deploy_failed(env):
    await env.handler.on_deploy(deploy("deploy", "c" * 40))
    assert env.link.sent[0]["data"] == {
        "version": 4,
        "commit_sha": "c" * 40,
        "code": "E_COMMIT_MISSING",
        "message": env.link.sent[0]["data"]["message"],
    }


async def test_remove_a_version_and_one_not_here(env):
    found = install(env.programs, manifest("sample"), DONE_PROGRAM, sha="c" * 40)
    await env.handler.on_deploy(deploy("remove", "c" * 40))
    await env.handler.on_deploy(deploy("remove", "c" * 40))
    assert not found.root.exists() and env.link.sent == []


async def test_deploy_without_a_link_says_nothing(env):
    env.handler.link = None
    install(env.programs, manifest("sample"), DONE_PROGRAM, sha="c" * 40)
    await env.handler.on_deploy(deploy("activate", "c" * 40))
    await env.handler.on_deploy(deploy("activate", "d" * 40))
    assert env.link.sent == []
