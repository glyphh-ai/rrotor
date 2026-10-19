"""The Cloud's messages against a real runner and deployer; what goes up the link is kept by a fake link."""

from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

import rrotor
from rrotord.access import Access
from rrotord.deploy import Deployer, Tools
from rrotord.licence import Licence
from rrotord.link import protocol as p
from rrotord.link.handler import LinkHandler
from rrotord.models import Models
from rrotord.proxy import EgressProxy
from rrotord.runner.engine import Runner
from rrotord.runner.record import Records
from tests.fakes import DONE_MODEL, FakeLink, GitRepo, LocalLauncher, install, keys, manifest, ready, token

WAITS = "from rrotor import Result\n\ndef start(ctx, inputs):\n    return Result.needs_input('day?')\n"


class Env:
    def __init__(self, root: Path, sock: Path) -> None:
        self.models = Models(root / "data")
        self.deployer = Deployer(
            self.models,
            root / "scratch",
            Tools(python=sys.executable, uv=shutil.which("uv") or "uv", github=f"file://{root / 'github'}"),
        )
        self.handler = LinkHandler(self.models, cast(Runner, None), self.deployer)
        self.runner = Runner(
            models=self.models,
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


def call(model: str = "sample", function: str = "start", run_id: str | None = None) -> p.Call:
    return p.Call(
        type="call",
        run_id=uuid.UUID(run_id or str(uuid.uuid4())),
        model=model,
        version=1,
        function=function,
        inputs={},
        caller="routine",
    )


async def test_deployed_is_the_active_versions(env):
    install(env.models, manifest("sample"), DONE_MODEL)
    assert env.handler.deployed() == [{"model": "sample", "version": 1, "commit_sha": "a" * 40}]


async def test_a_call_answers_its_result_up_the_link(env):
    install(env.models, manifest("sample"), DONE_MODEL)
    message = call()
    await env.handler.on_call(message)
    result = env.results()[0]
    assert (result["run_id"], result["result"]["status"], result["seq"]) == (str(message.run_id), "done", 1)


async def test_a_call_that_is_refused_answers_failed(env):
    message = call(model="missing")
    await env.handler.on_call(message)
    assert env.results()[0]["result"]["data"]["code"] == "E_MODEL_MISSING"


async def test_continue_and_cancel_of_a_waiting_run(env):
    install(env.models, manifest("sample"), WAITS)
    message = call()
    await env.handler.on_call(message)
    await env.handler.on_continue(p.Continue(type="continue", run_id=message.run_id, answer="x"))
    await env.handler.on_cancel(p.Cancel(type="cancel", run_id=message.run_id))
    assert [r["result"]["status"] for r in env.results()] == ["needs_input", "needs_input", "cancelled"]


async def test_continue_of_a_run_not_here(env):
    await env.handler.on_continue(p.Continue(type="continue", run_id=uuid.uuid4(), answer=1))
    assert env.results()[0]["result"]["data"]["code"] == "E_RUN_MISSING"


async def test_continue_of_a_run_that_ended_is_only_logged(env, caplog):
    install(env.models, manifest("sample"), DONE_MODEL)
    message = call()
    await env.handler.on_call(message)
    await env.handler.on_continue(p.Continue(type="continue", run_id=message.run_id, answer=1))
    assert len(env.results()) == 1 and "continue refused" in caplog.text


async def test_cancel_of_a_run_not_here_answers_cancelled(env):
    await env.handler.on_cancel(p.Cancel(type="cancel", run_id=uuid.uuid4()))
    assert env.results()[0]["result"] == {"status": "cancelled", "data": None, "step": "cancel", "prompt": None}


async def test_cancel_of_an_ended_run_answers_nothing(env):
    install(env.models, manifest("sample"), DONE_MODEL)
    message = call()
    await env.handler.on_call(message)
    await env.handler.on_cancel(p.Cancel(type="cancel", run_id=message.run_id))
    assert len(env.results()) == 1


async def test_local_runs_and_a_handler_without_a_link_send_nothing(env):
    install(env.models, manifest("sample"), DONE_MODEL)
    await env.runner.call(model="sample", function="start", inputs={})
    env.handler.link = None
    await env.handler.on_call(call())
    await env.handler.on_call(call(model="missing"))
    assert env.link.sent == []


def deploy(kind: str, sha: str, model: str = "sample", repo: str | None = None) -> p.Deploy:
    return p.Deploy(type=kind, model=model, version=4, commit_sha=sha, repo=repo, token=token() if repo else None)


async def test_deploy_from_the_repo_answers_deployed(env, tmp_path):
    repo = GitRepo(tmp_path / "src")
    sha = repo.model(manifest("sample"))
    repo.bare(tmp_path / "github", "owner/sample")
    await env.handler.on_deploy(deploy("deploy", sha, repo="owner/sample"))
    assert env.link.sent == [
        {
            "type": "deployed",
            "id": env.link.sent[0]["id"],
            "model": "sample",
            "version": 4,
            "commit_sha": sha,
            "repo": "owner/sample",
            "manifest": env.models.active("sample").manifest.model_dump(mode="json"),
        }
    ]
    assert env.link.sent[0]["manifest"]["name"] == "sample"
    assert env.models.active("sample").version == 4


async def test_deploy_of_a_commit_naming_another_model_is_refused(env, tmp_path):
    repo = GitRepo(tmp_path / "src")
    sha = repo.model(manifest("other"))
    repo.bare(tmp_path / "github", "owner/sample")
    await env.handler.on_deploy(deploy("deploy", sha, repo="owner/sample"))
    event = env.link.sent[0]
    assert (event["type"], event["kind"], event["data"]["code"]) == ("event", "deploy_failed", "E_MANIFEST")
    assert env.models.versions("other") == []


async def test_activate_a_held_commit(env):
    install(env.models, manifest("sample"), DONE_MODEL, sha="c" * 40)
    await env.handler.on_deploy(deploy("activate", "c" * 40))
    sent = env.link.sent[0]
    assert sent["type"] == "deployed" and sent["version"] == 4 and sent["repo"] is None
    assert sent["manifest"]["name"] == "sample"


async def test_a_deploy_is_reported_only_with_a_link_that_has_a_runtime_token(env):
    version = install(env.models, manifest("sample"), DONE_MODEL, sha="c" * 40)
    off = FakeLink(configured=False)
    env.handler.link = cast(Any, off)
    await env.handler.report(version, None)
    assert off.sent == []
    env.handler.link = None
    await env.handler.report(version, "owner/sample")
    env.handler.link = cast(Any, env.link)
    await env.handler.report(version, "owner/sample")
    assert [(m["type"], m["repo"], m["commit_sha"]) for m in env.link.sent] == [("deployed", "owner/sample", "c" * 40)]
    assert env.link.seq == 0  # not numbered


async def test_activate_a_commit_not_held_answers_deploy_failed(env):
    await env.handler.on_deploy(deploy("deploy", "c" * 40))
    assert env.link.sent[0]["data"] == {
        "version": 4,
        "commit_sha": "c" * 40,
        "code": "E_COMMIT_MISSING",
        "message": env.link.sent[0]["data"]["message"],
    }


async def test_remove_a_version_and_one_not_here(env):
    found = install(env.models, manifest("sample"), DONE_MODEL, sha="c" * 40)
    await env.handler.on_deploy(deploy("remove", "c" * 40))
    await env.handler.on_deploy(deploy("remove", "c" * 40))
    assert not found.root.exists() and env.link.sent == []


async def test_deploy_without_a_link_says_nothing(env):
    env.handler.link = None
    install(env.models, manifest("sample"), DONE_MODEL, sha="c" * 40)
    await env.handler.on_deploy(deploy("activate", "c" * 40))
    await env.handler.on_deploy(deploy("activate", "d" * 40))
    assert env.link.sent == []


async def test_a_welcomes_licence_key_is_offered_preferred_to_its_legacy_licence_and_a_bad_one_logged(
    env, tmp_path, caplog
):
    await env.handler.on_welcome(p.Welcome(type="welcome", licence=p.Signed(payload={"plan": "Pro"}, signature="x")))
    env.handler.licence = Licence(tmp_path / "lic")
    await env.handler.on_welcome(p.Welcome(type="welcome"))
    assert env.handler.licence.state == "none"
    given = keys().licence(["ada"], plan="Team")
    await env.handler.on_welcome(p.Welcome(type="welcome", licence=p.Signed(**given)))
    assert env.handler.licence.limits().tier == "team"  # release N: the legacy licence alone, mapped
    key = keys().key(tier="pro")
    await env.handler.on_welcome(p.Welcome(type="welcome", licence=p.Signed(**given), license_key=key))
    assert env.handler.licence.limits().tier == "pro" and env.handler.licence.view()["source"] == "welcome"
    with caplog.at_level("ERROR", logger="rrotor.licence"):
        await env.handler.on_welcome(p.Welcome(type="welcome", license_key=keys().key(drop=("tier",))))
    assert "licence key refused: tier missing" in caplog.text and env.handler.licence.limits().tier == "pro"


async def test_keys_and_the_pause_reach_the_access_store(env, tmp_path):
    await env.handler.on_access(p.Keys(type="keys", hashes=["a" * 64]))  # no store: nothing
    await env.handler.on_welcome(p.Welcome(type="welcome", keys=["a" * 64]))
    env.handler.access = Access(tmp_path / "access")
    await env.handler.on_welcome(p.Welcome(type="welcome", keys=["a" * 64]))
    assert env.handler.access.keys == ["a" * 64]
    await env.handler.on_access(p.Keys(type="keys", hashes=["b" * 64, "c" * 64]))
    assert env.handler.access.keys == ["b" * 64, "c" * 64]
    await env.handler.on_access(p.Pause(type="pause", reason="runtime_hours"))
    assert env.handler.access.paused == {"reason": "runtime_hours"}
    await env.handler.on_access(p.Resume(type="resume"))
    assert env.handler.access.paused is None
