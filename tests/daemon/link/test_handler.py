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
from rrotord.catalogue import Catalogue
from rrotord.deploy import Deployer, Tools
from rrotord.licence import Licence
from rrotord.link import protocol as p
from rrotord.link.handler import LinkHandler
from rrotord.programs import Programs
from rrotord.proxy import EgressProxy
from rrotord.runner.engine import Runner
from rrotord.runner.record import Records
from tests.fakes import DONE_PROGRAM, FakeLink, GitRepo, LocalLauncher, install, keys, manifest, ready, token

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


async def test_a_call_carries_the_folders_its_run_is_given(env, tmp_path):
    from rrotord.folders import Folders

    docs = tmp_path.resolve() / "docs"
    docs.mkdir()
    env.runner.folders = Folders((docs,))
    sees = "from rrotor import Result\n\ndef start(ctx, inputs):\n    return Result.done(list(ctx.folders))\n"
    install(env.programs, manifest("sample", folders=True), sees)
    given = [{"path": str(docs), "access": "write"}]
    await env.handler.on_call(p.Call.model_validate({**call().model_dump(), "folders": given}))
    assert env.results()[0]["result"] == {"status": "done", "data": given, "step": None, "prompt": None}
    # a folder the owner does not offer: the call is refused, saying why in a plain sentence
    asked = [{"path": str(tmp_path.resolve()), "access": "read"}]
    await env.handler.on_call(p.Call.model_validate({**call().model_dump(), "folders": asked}))
    refused = env.results()[1]["result"]
    assert (refused["status"], refused["data"]["code"]) == ("failed", "E_FOLDER_REFUSED")
    assert refused["data"]["reason"] == (
        f"{tmp_path.resolve()} is not in a folder this rrotor's owner shares with bots: it shares {docs}."
    )


async def test_a_call_with_no_version_runs_the_active_one_a_builtin_program_too(env, tmp_path):
    from tests.daemon.test_builtin_clis import builtin_folder

    await env.deployer.builtin(builtin_folder(tmp_path / "clis", "claude"))
    await env.deployer.builtin(Path(__file__).resolve().parents[3] / "programs" / "glyphh_bot")
    message = p.Call.model_validate(
        {
            "type": "call",
            "run_id": str(uuid.uuid4()),
            "program": "glyphh_bot",
            "version": None,  # built-ins are not in the hello's deployed: the server cannot know their versions
            "function": "run",
            "inputs": {
                "package": {"bot.json": {}},
                "ada_base": "https://ada.glyphh.ai",
                "ada_key": "k",
                "tool_url": "https://platform.glyphh.ai/v1/bots/runs/1/tool",
                "tool_token": "t",
            },
            "caller": "routine",
        }
    )
    assert message.version is None
    await env.handler.on_call(message)
    result = env.results()[0]["result"]
    # it ran glyphh_bot's own code: this test's Python has no Glyphh SDK, which the program says itself
    assert (result["status"], result["data"]["code"]) == ("failed", "E_SDK"), result


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
        {
            "type": "deployed",
            "id": env.link.sent[0]["id"],
            "program": "sample",
            "version": 4,
            "commit_sha": sha,
            "repo": "owner/sample",
            "manifest": env.programs.active("sample").manifest.model_dump(mode="json"),
            "by": None,
        }
    ]
    assert env.link.sent[0]["manifest"]["name"] == "sample"
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
    sent = env.link.sent[0]
    assert sent["type"] == "deployed" and sent["version"] == 4 and sent["repo"] is None
    assert sent["manifest"]["name"] == "sample"


async def test_a_deploy_is_reported_only_with_a_link_that_has_a_runtime_token(env):
    version = install(env.programs, manifest("sample"), DONE_PROGRAM, sha="c" * 40)
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


APP = {"name": "board", "kind": "app", "app": {"title": "Board"}}


def app_versions(env: Env, n: int) -> None:
    for i in range(1, n + 1):
        install(env.programs, APP, "", sha=str(i) * 40, version=i)


async def test_the_catalogues_word_on_an_app_is_kept_and_an_approved_version_activated(env, tmp_path):
    env.handler.catalogue = Catalogue(tmp_path / "data")
    app_versions(env, 2)
    assert env.programs.active("board").version == 2
    await env.handler.on_app(p.App(type="app", program="board", version=1, state="approved"))
    assert env.handler.catalogue.get("board") == {"version": 1, "state": "approved", "roles": []}
    assert env.programs.active("board").version == 1  # the version approved is the one served
    await env.handler.on_app(p.App(type="app", program="board", version=1, state="approved"))
    assert env.programs.active("board").version == 1  # already active: nothing more
    await env.handler.on_app(p.App(type="artifact", program="board", version=2, state="rejected"))  # protocol 2's
    assert env.programs.active("board").version == 1 and env.handler.catalogue.state("board", 2) == "rejected"
    await env.handler.on_app(p.App(type="app", program="board", version=9, state="approved"))
    assert env.programs.active("board").version == 1  # a version not held here: kept, nothing activated
    await env.handler.on_app(p.App(type="app", program="nope", version=1, state="approved"))
    assert env.handler.catalogue.get("nope") == {"version": 1, "state": "approved", "roles": []}
    env.handler.catalogue = None
    await env.handler.on_app(p.App(type="app", program="board", version=2, state="approved"))
    assert env.programs.active("board").version == 1  # no catalogue here: ignored


async def test_a_welcome_carries_the_catalogues_whole_word_and_the_reviewers(env, tmp_path):
    env.handler.catalogue = Catalogue(tmp_path / "data")
    app_versions(env, 2)
    reviewed = [
        p.Reviewed(program="board", version=1, state="approved", roles=["sre"]),
        p.Reviewed(program="other", version=3, state="rejected"),
    ]
    await env.handler.on_welcome(p.Welcome(type="welcome", apps=reviewed, reviewers=["Ops"]))
    assert env.handler.catalogue.get("board") == {"version": 1, "state": "approved", "roles": ["sre"]}
    assert env.handler.catalogue.get("other") == {"version": 3, "state": "rejected", "roles": []}
    assert env.handler.catalogue.reviewers == ["ops"] and env.programs.active("board").version == 1
    await env.handler.on_welcome(p.Welcome(type="welcome"))  # an older server: nothing said, nothing changed
    assert env.handler.catalogue.get("board") is not None
    old = {"type": "welcome", "artifacts": [{"program": "board", "version": 2, "state": "approved"}]}  # protocol 2's
    await env.handler.on_welcome(p.Welcome.model_validate(old))
    assert env.handler.catalogue.get("board") == {"version": 2, "state": "approved", "roles": []}
    assert env.programs.active("board").version == 2


async def test_report_carries_who_published(env):
    install(env.programs, APP, "", sha="1" * 40)
    await env.handler.report(env.programs.active("board"), None, "user_1")
    assert env.link.sent[0]["by"] == "user_1" and env.link.sent[0]["manifest"]["kind"] == "app"


async def test_a_welcomes_licence_key_is_offered_preferred_to_its_legacy_licence_and_a_bad_one_logged(
    env, tmp_path, caplog
):
    await env.handler.on_welcome(p.Welcome(type="welcome", licence=p.Signed(payload={"plan": "Pro"}, signature="x")))
    env.handler.licence = Licence(tmp_path / "lic")
    await env.handler.on_welcome(p.Welcome(type="welcome"))
    assert env.handler.licence.state == "none"
    given = keys().licence(["rrotor"], plan="Team")
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


async def test_what_the_cloud_asks_for_is_audited_as_the_servers(env, tmp_path):
    from rrotord.audit import Audit

    audit = Audit(tmp_path / "audit", tier=lambda: "enterprise")
    env.handler.audit = env.runner.audit = audit
    install(env.programs, manifest("sample"), WAITS, sha="c" * 40)
    message = call()
    await env.handler.on_call(message)
    await env.handler.on_continue(p.Continue(type="continue", run_id=message.run_id, answer="x"))
    await env.handler.on_cancel(p.Cancel(type="cancel", run_id=message.run_id))
    await env.handler.on_deploy(deploy("activate", "c" * 40))
    await env.handler.on_deploy(deploy("deploy", "d" * 40))
    await env.handler.on_deploy(deploy("remove", "c" * 40))
    events = audit.read(None, 100)[0]
    assert [e["kind"] for e in events] == [
        "run.call",
        "run.continue",
        "run.end",
        "run.cancel",
        "program.activate",
        "program.deploy_failed",
        "program.remove",
    ]
    assert {(e["actor"]["type"], e["via"]) for e in events} == {("server", "link")}
    assert events[5]["outcome"] == {"status": "failed", "code": "E_COMMIT_MISSING"}
    assert (events[4]["program"], events[4]["version"], events[6]["commit_sha"]) == ("sample", 4, "c" * 40)


async def test_a_repo_deploy_from_the_cloud_is_program_deploy(env, tmp_path):
    from rrotord.audit import Audit

    audit = Audit(tmp_path / "audit", tier=lambda: "enterprise")
    env.handler.audit = audit
    repo = GitRepo(tmp_path / "src")
    sha = repo.program(manifest("sample"))
    repo.bare(tmp_path / "github", "owner/sample")
    await env.handler.on_deploy(deploy("deploy", sha, repo="owner/sample"))
    event = audit.read(None, 10)[0][0]
    assert (event["kind"], event["repo"], event["commit_sha"]) == ("program.deploy", "owner/sample", sha)
