"""The runner end to end: real worker processes (the host's Python, no sandbox) running small programs."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

import pytest

import rrotor
from rrotord.errors import BadRequest, Conflict, NotFound, Unavailable
from rrotord.programs import Programs
from rrotord.proxy import EgressProxy
from rrotord.runner import engine
from rrotord.runner.engine import Runner
from rrotord.runner.record import Records, Run
from rrotord.sandbox.probe import Layers
from tests.fakes import LocalLauncher, install, manifest, ready

APPROVE = """
from rrotor import Result

def start(ctx, inputs):
    n = ctx.checkpoint("n", lambda: inputs.get("n", 0) + 1)
    if not ctx.ask_approval("write", "Write it?"):
        return Result.done({"written": False, "n": n})
    return Result.done({"written": True, "n": n})
"""

WAIT_RESULT = """
from rrotor import Result

def start(ctx, inputs):
    if ctx.answer is None:
        return Result.needs_input("Which day?")
    return Result.done(ctx.answer)
"""


class Env:
    def __init__(self, root: Path, sock: Path, launcher: Any = None, layers: Layers | None = None) -> None:
        self.programs = Programs(root)
        self.records = Records(root)
        self.told: list[tuple[str, str]] = []
        self.launcher = launcher or LocalLauncher()
        self.proxy = EgressProxy(sock)
        self.runner = self.make(layers)

    def make(self, layers: Layers | None = None, wait_limit: float = engine.WAIT_LIMIT) -> Runner:
        async def notify(run: Run) -> None:
            self.told.append((run.id, run.status))

        return Runner(
            programs=self.programs,
            records=self.records,
            proxy=self.proxy,
            launcher=self.launcher,
            sdk=Path(rrotor.__file__).parent,
            layers=layers or ready(),
            notify=notify,
            wait_limit=wait_limit,
        )

    def deploy(self, source: str, **extra: Any) -> None:
        data = manifest("sample", **{"functions": {"start": {"approvals": ["write"]}}, **extra})
        install(self.programs, data, source)

    def events(self, run: Run) -> list[str]:
        return [line.get("event", "") for line in self.records.lines(run.id)]


@pytest.fixture
async def env(tmp_path, short_dir):
    e = Env(tmp_path / "data", short_dir)
    yield e
    await e.runner.shutdown()


async def test_a_call_answers_done_and_ends(env):
    env.deploy(
        "from rrotor import Result\n\ndef start(ctx, inputs):\n    print('hi')\n    return Result.done(inputs)\n"
    )
    run = await env.runner.call(program="sample", function="start", inputs={"a": 1})
    assert (run.status, run.result["data"], run.final) == ("done", {"a": 1}, True)
    assert env.told == [(run.id, "done")] and run.id not in env.runner.workers
    assert {"stream": "stdout", "line": "hi"} in [
        {k: v for k, v in line.items() if k != "at"} for line in env.records.lines(run.id)
    ]
    assert env.events(run)[0] == "called" and env.events(run)[-1] == "ended"


async def test_a_waiting_run_continues_in_the_same_worker(env):
    env.deploy(APPROVE)
    run = await env.runner.call(program="sample", function="start", inputs={"n": 1})
    assert (run.status, run.result["step"]) == ("needs_approval", "write")
    assert env.runner.approvals()[0]["step"] == "write"
    assert run.state["checkpoints"] == {"n": 2}
    run = await env.runner.resume(run.id, {"approved": True})
    assert (run.status, run.result["data"]) == ("done", {"written": True, "n": 2})
    assert env.told == [(run.id, "needs_approval"), (run.id, "done")]


async def test_a_declined_approval(env):
    env.deploy(APPROVE)
    run = await env.runner.call(program="sample", function="start", inputs={})
    run = await env.runner.resume(run.id, {"approved": False})
    assert run.result["data"] == {"written": False, "n": 1}


async def test_a_returned_wait_reads_the_answer(env):
    env.deploy(WAIT_RESULT)
    run = await env.runner.call(program="sample", function="start", inputs={})
    assert run.status == "needs_input"
    assert (await env.runner.resume(run.id, "2026-10-14")).result["data"] == "2026-10-14"


async def test_after_a_restart_a_continue_starts_a_new_worker_with_the_checkpoints(env):
    env.deploy(APPROVE)
    run = await env.runner.call(program="sample", function="start", inputs={"n": 5})
    await env.runner.shutdown()
    restarted = env.make()
    assert await restarted.recover() == []
    run = await restarted.resume(run.id, {"approved": True})
    assert run.result["data"] == {"written": True, "n": 6}


async def test_recover_fails_runs_that_were_running(env):
    env.deploy(APPROVE)
    run = Run("6f1c2d4e-0000-4000-8000-000000000001", "sample", 1, "a" * 40, "start", {}, "claude", "local")
    env.records.save(run)
    assert await env.runner.recover() == [run.id]
    assert env.runner.get(run.id).result["data"]["code"] == "E_RESTARTED"


async def test_resume_a_run_that_is_not_waiting(env):
    env.deploy(WAIT_RESULT)
    run = await env.runner.call(program="sample", function="start", inputs={})
    await env.runner.resume(run.id, 1)
    with pytest.raises(Conflict) as caught:
        await env.runner.resume(run.id, 1)
    assert caught.value.code == "E_RUN_STATE"


async def test_resume_a_run_whose_version_is_gone(env):
    env.deploy(WAIT_RESULT)
    run = await env.runner.call(program="sample", function="start", inputs={})
    env.programs.remove(env.programs.get("sample", 1))
    run = await env.runner.resume(run.id, 1)
    assert run.result["data"]["code"] == "E_VERSION_MISSING"


async def test_a_run_not_here(env):
    with pytest.raises(NotFound) as caught:
        env.runner.get("6f1c2d4e-0000-4000-8000-000000000002")
    assert caught.value.code == "E_RUN_MISSING"


async def test_cancel_a_waiting_run_runs_end(env):
    env.deploy(WAIT_RESULT + "\ndef end(ctx):\n    ctx.log('bye')\n")
    run = await env.runner.call(program="sample", function="start", inputs={})
    run = await env.runner.cancel(run.id)
    assert (run.status, run.result["step"]) == ("cancelled", "cancel")
    assert any(line.get("message") == "bye" for line in env.records.lines(run.id))
    with pytest.raises(Conflict):
        await env.runner.cancel(run.id)


async def test_cancel_a_running_call(env):
    env.deploy("import time\n\ndef start(ctx, inputs):\n    ctx.log('going')\n    time.sleep(20)\n")
    call = asyncio.ensure_future(env.runner.call(program="sample", function="start", inputs={}, run_id=RUN_ID))
    for _ in range(200):
        if any(line.get("message") == "going" for line in env.records.lines(RUN_ID)):
            break
        await asyncio.sleep(0.02)
    run = await env.runner.cancel(RUN_ID)
    assert run.status == "cancelled"
    assert (await call).status == "cancelled"


RUN_ID = "6f1c2d4e-0000-4000-8000-000000000003"


async def test_cancel_a_waiting_run_whose_worker_is_gone(env):
    env.deploy(WAIT_RESULT)
    run = await env.runner.call(program="sample", function="start", inputs={})
    restarted = env.make()
    assert (await restarted.cancel(run.id)).status == "cancelled"


async def test_a_function_past_its_seconds_fails(env):
    env.deploy("import time\n\ndef start(ctx, inputs):\n    time.sleep(20)\n", limits={"seconds": 1})
    run = await env.runner.call(program="sample", function="start", inputs={})
    assert run.result["data"]["code"] == "E_LIMIT_SECONDS"


async def test_a_worker_that_does_not_stop_itself_is_stopped(env, monkeypatch):
    monkeypatch.setattr(engine, "GRACE", 0.2)
    env.deploy(
        "import signal, time\n\ndef load(ctx):\n    signal.signal(signal.SIGALRM, signal.SIG_IGN)\n\n"
        "def start(ctx, inputs):\n    time.sleep(20)\n",
        limits={"seconds": 1},
    )
    run = await env.runner.call(program="sample", function="start", inputs={})
    assert run.result["data"] == {"reason": "The run went past its seconds and was stopped.", "code": "E_LIMIT_SECONDS"}


async def test_a_worker_that_dies_fails_the_run(env):
    env.deploy("import os\n\ndef start(ctx, inputs):\n    os._exit(3)\n")
    run = await env.runner.call(program="sample", function="start", inputs={})
    assert run.result["data"] == {"reason": "The program's process stopped (exit 3).", "code": "E_WORKER"}


async def test_an_end_that_fails_is_logged(env):
    env.deploy(
        "from rrotor import Result\n\ndef start(ctx, inputs):\n    return Result.done()\n\ndef end(ctx):\n    1 / 0\n"
    )
    run = await env.runner.call(program="sample", function="start", inputs={})
    assert run.status == "done" and "end failed" in env.events(run)


async def test_a_sandbox_that_does_not_start_fails_the_run(tmp_path, short_dir):
    e = Env(tmp_path, short_dir, launcher=LocalLauncher(fail=OSError("no bwrap")))
    e.deploy(WAIT_RESULT, hosts=["api.example.com"])
    run = await e.runner.call(program="sample", function="start", inputs={})
    assert run.result["data"]["code"] == "E_SANDBOX"  # type: ignore[index]
    assert not (short_dir / f"{run.id}.sock").exists()


async def test_a_program_with_hosts_gets_a_proxy_and_its_data_folder(env):
    env.deploy(WAIT_RESULT, hosts=["api.example.com"], data=True)
    run = await env.runner.call(program="sample", function="start", inputs={})
    assert env.launcher.specs[0].mounts.proxy is not None and env.programs.data("sample").is_dir()
    assert env.launcher.specs[0].mounts.data == env.programs.data("sample")
    await env.runner.resume(run.id, "x")
    assert run.id not in env.proxy.servers


async def test_a_program_that_needs_a_part_fails(env):
    env.deploy(WAIT_RESULT, databases=["postgres", "neo4j"])
    run = await env.runner.call(program="sample", function="start", inputs={})
    assert run.result["data"]["code"] == "E_PART_MISSING"
    assert "needs the parts postgres, neo4j" in run.result["data"]["reason"]


async def test_a_program_that_needs_one_part(env):
    env.deploy(WAIT_RESULT, profiles={"work": {}})
    run = await env.runner.call(program="sample", function="start", inputs={})
    assert "needs the part programs," in run.result["data"]["reason"]


async def test_no_run_while_the_sandbox_is_not_ready(tmp_path, short_dir):
    e = Env(tmp_path, short_dir, layers=Layers({"landlock": {"ok": False, "detail": "none"}}))
    with pytest.raises(Unavailable) as caught:
        await e.runner.call(program="sample", function="start", inputs={})
    assert caught.value.code == "E_SANDBOX_UNAVAILABLE" and "landlock: none" in caught.value.message


async def test_a_function_not_in_the_manifest(env):
    env.deploy(WAIT_RESULT)
    with pytest.raises(NotFound) as caught:
        await env.runner.call(program="sample", function="other", inputs={})
    assert caught.value.code == "E_FUNCTION_MISSING"


async def test_inputs_refused_by_the_schema(env):
    env.deploy(WAIT_RESULT, functions={"start": {"inputs": {"type": "object", "required": ["day"]}}})
    with pytest.raises(BadRequest):
        await env.runner.call(program="sample", function="start", inputs={})


async def test_waiting_runs_expire_after_the_limit(env):
    env.deploy(WAIT_RESULT)
    waiting = await env.runner.call(program="sample", function="start", inputs={})
    later = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=16)
    assert await env.runner.expire(dt.datetime.now(dt.UTC)) == []
    assert await env.runner.expire(later) == [waiting.id]
    assert env.runner.get(waiting.id).status == "expired"
    assert await env.runner.expire(later) == []


async def test_a_run_that_stopped_waiting_meanwhile_does_not_expire(env, monkeypatch):
    env.deploy(WAIT_RESULT)
    waiting = await env.runner.call(program="sample", function="start", inputs={})
    real = env.runner.get
    calls = []

    def get(run_id: str) -> Run:
        found: Run = real(run_id)
        calls.append(run_id)
        found.status = "done"
        return found

    monkeypatch.setattr(env.runner, "get", get)
    assert await env.runner.expire(dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)) == []
    assert calls == [waiting.id]


async def test_without_notify_nothing_is_told(tmp_path, short_dir):
    e = Env(tmp_path, short_dir)
    e.runner.notify = None
    e.deploy(WAIT_RESULT)
    await e.runner.call(program="sample", function="start", inputs={})
    assert e.told == []
    await e.runner.shutdown()


def test_notes_keep_checkpoints_logs_and_output(env):
    run = Run(RUN_ID, "sample", 1, "a" * 40, "start", {}, "claude", "local")
    env.runner._note(run, {"op": "checkpoint", "key": "k", "value": 1})
    env.runner._note(run, {"op": "log", "message": "m"})
    env.runner._note(run, {"op": "other", "stream": "stdout", "line": "x"})
    assert run.state["checkpoints"] == {"k": 1}
    lines = env.records.lines(RUN_ID)
    assert (lines[0]["message"], lines[0]["detail"], lines[1]["line"]) == ("m", {}, "x")
