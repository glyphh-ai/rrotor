"""The scheduler with a clock the tests move: due schedules start as normal runs (caller `schedule`) of the active
version, missed fires are not replayed, a schedule whose last run has not ended is skipped and the skip kept, a pause
survives a restart, only the head fires, and a refused call is the schedule's last result."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

import pytest

from rrotord.audit import Audit
from rrotord.errors import NotFound
from rrotord.sandbox.probe import Layers
from rrotord.scheduler import Scheduler
from rrotord.store import write_json
from tests.daemon.runner.test_engine import APPROVE, Env
from tests.fakes import install, manifest

ECHO = "from rrotor import Result\n\ndef start(ctx, inputs):\n    return Result.done([ctx.caller, inputs])\n"


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, second, tzinfo=dt.UTC)


class Clock:
    def __init__(self, *parts: int) -> None:
        self.now = utc(*parts)

    def __call__(self) -> dt.datetime:
        return self.now

    def to(self, *parts: int) -> None:
        self.now = utc(*parts)


@pytest.fixture
async def env(tmp_path, short_dir):
    e = Env(tmp_path / "data", short_dir)
    yield e
    await e.runner.shutdown()


def deploy(env: Env, schedules: list[dict[str, Any]], source: str = ECHO, sha: str = "a" * 40, version: int = 1):
    functions = {"start": {"approvals": ["write"]}}
    install(
        env.programs, manifest("sample", functions=functions, schedules=schedules), source, sha=sha, version=version
    )


async def settle(s: Scheduler) -> None:
    await asyncio.gather(*s.tasks.values())


def make(env: Env, clock: Clock, **kw: Any) -> Scheduler:
    return Scheduler(env.programs, env.runner, env.programs.root.parent, clock=clock, **kw)


async def test_a_due_schedule_runs_the_active_version_and_nothing_missed_is_replayed(env):
    deploy(env, [{"function": "start", "cron": "0 9 * * *", "input": {"day": "x"}}])
    clock = Clock(2026, 10, 31, 9, 0, 30)  # started just past 09:00: today's is missed, not replayed
    s = make(env, clock)
    assert await s.tick() == []
    [view] = s.view()
    assert (view["id"], view["name"], view["next_at"], view["last"], view["paused"]) == (
        "sample/start",
        "start",
        "2026-11-01T09:00:00Z",
        None,
        False,
    )
    clock.to(2026, 11, 1, 8, 59)
    assert await s.tick() == []
    clock.to(2026, 11, 1, 9, 0, 0)
    assert await s.tick() == ["sample/start"]
    await settle(s)
    [run] = env.records.all()
    assert (run.caller, run.function, run.version, run.inputs, run.status) == (
        "schedule",
        "start",
        1,
        {"day": "x"},
        "done",
    )
    assert run.result is not None and run.result["data"] == ["schedule", {"day": "x"}]
    [view] = s.view()
    assert view["last"] == {"run_id": run.id, "at": "2026-11-01T09:00:00Z", "status": "done", "code": None}
    assert view["next_at"] == "2026-11-02T09:00:00Z"
    clock.to(2026, 11, 1, 9, 1)
    assert await s.tick() == []  # once a day


async def test_a_tick_late_by_minutes_fires_once(env):
    deploy(env, [{"function": "start", "cron": "* * * * *"}])
    clock = Clock(2026, 10, 31, 9, 0, 0)
    s = make(env, clock)
    await s.tick()
    clock.to(2026, 10, 31, 9, 5, 0)  # the loop was busy: five fires due, one run
    assert await s.tick() == ["sample/start"]
    await settle(s)
    assert len(env.records.all()) == 1


async def test_a_schedule_still_waiting_is_skipped_and_the_skip_kept(env, tmp_path):
    deploy(env, [{"function": "start", "cron": "* * * * *"}], APPROVE)
    audit = Audit(tmp_path / "audit", tier=lambda: "enterprise")
    env.runner.audit = audit
    clock = Clock(2026, 10, 31, 9, 0, 0)
    s = make(env, clock, audit=audit)
    await s.tick()
    clock.to(2026, 10, 31, 9, 1, 0)
    assert await s.tick() == ["sample/start"]
    clock.to(2026, 10, 31, 9, 1, 30)
    assert await s.tick() == []  # its task is still going
    await settle(s)
    [run] = env.records.all()
    assert run.status == "needs_approval" and env.runner.approvals()[0]["run_id"] == run.id
    clock.to(2026, 10, 31, 9, 2, 0)
    assert await s.tick() == []
    [view] = s.view()
    assert view["last"]["status"] == "needs_approval"
    assert view["skipped"] == {"at": "2026-10-31T09:02:00Z", "run_id": run.id, "count": 1}
    events = audit.query({"kind": "schedule.skipped"})["events"]
    assert events[0]["schedule"] == "sample/start" and events[0]["actor"] == {"type": "rrotor"}
    assert events[0]["via"] == "schedule"
    called = audit.query({"kind": "run.call"})["events"][0]
    assert called["via"] == "schedule" and called["run_id"] == run.id
    await env.runner.resume(run.id, {"approved": True})
    assert s.view()[0]["last"]["status"] == "done"
    clock.to(2026, 10, 31, 9, 3, 0)
    assert await s.tick() == ["sample/start"]
    await settle(s)
    assert s.view()[0]["skipped"]["count"] == 1


async def test_a_pause_is_kept_across_a_restart(env, tmp_path):
    deploy(env, [{"function": "start", "cron": "*/10 * * * *", "name": "often"}])
    audit = Audit(tmp_path / "audit", tier=lambda: "enterprise")
    clock = Clock(2026, 10, 31, 9, 5, 0)
    s = make(env, clock, audit=audit)
    await s.tick()
    paused = s.pause("sample", "often", True)
    assert (paused["paused"], paused["next_at"]) == (True, None)
    clock.to(2026, 10, 31, 9, 10, 0)
    assert await s.tick() == []
    again = make(env, clock)
    assert again.view()[0]["paused"] is True
    resumed = again.pause("sample", "often", False)
    assert (resumed["paused"], resumed["next_at"]) == (False, "2026-10-31T09:20:00Z")
    clock.to(2026, 10, 31, 9, 10, 30)
    await again.tick()
    clock.to(2026, 10, 31, 9, 20, 0)
    assert await again.tick() == ["sample/often"]
    await settle(again)
    with pytest.raises(NotFound) as missing:
        s.pause("sample", "nothing", True)
    assert missing.value.code == "E_SCHEDULE_MISSING"
    kinds = [e["kind"] for e in audit.query({})["events"]]
    assert "schedule.pause" in kinds


async def test_only_the_head_fires_and_a_new_head_starts_afresh(env):
    deploy(env, [{"function": "start", "cron": "* * * * *"}])
    clock = Clock(2026, 10, 31, 9, 0, 0)
    head = {"is": False}
    s = make(env, clock, leading=lambda: head["is"])
    await s.tick()
    clock.to(2026, 10, 31, 9, 3, 0)
    assert await s.tick() == [] and s.due == {}
    head["is"] = True
    clock.to(2026, 10, 31, 9, 3, 30)
    assert await s.tick() == []  # what came due while it followed is not replayed
    clock.to(2026, 10, 31, 9, 4, 0)
    assert await s.tick() == ["sample/start"]
    await settle(s)


async def test_a_refused_call_is_the_schedules_last_result(env):
    deploy(env, [{"function": "start", "cron": "* * * * *"}])
    env.runner.layers = Layers({"bubblewrap": {"ok": False, "detail": "not installed"}})
    clock = Clock(2026, 10, 31, 9, 0, 0)
    s = make(env, clock)
    await s.tick()
    clock.to(2026, 10, 31, 9, 1, 0)
    await s.tick()
    await settle(s)
    last = s.view()[0]["last"]
    assert (last["status"], last["code"], last["run_id"]) == ("refused", "E_SANDBOX_UNAVAILABLE", None)
    clock.to(2026, 10, 31, 9, 2, 0)
    assert await s.tick() == ["sample/start"]  # a refused call is not a run still going
    await settle(s)


async def test_a_failed_run_shows_its_code(env):
    deploy(env, [{"function": "start", "cron": "* * * * *"}], "def start(ctx, inputs):\n    raise ValueError('no')\n")
    clock = Clock(2026, 10, 31, 9, 0, 0)
    s = make(env, clock)
    await s.tick()
    clock.to(2026, 10, 31, 9, 1, 0)
    await s.tick()
    await settle(s)
    assert (s.view()[0]["last"]["status"], s.view()[0]["last"]["code"]) == ("failed", "E_CRASH")


async def test_a_new_version_changes_the_schedules(env):
    deploy(env, [{"function": "start", "cron": "0 9 * * *"}])
    clock = Clock(2026, 10, 31, 8, 0, 0)
    s = make(env, clock)
    await s.tick()
    assert s.due["sample/start"][1] == dt.datetime(2026, 10, 31, 9, tzinfo=dt.UTC)
    deploy(env, [{"function": "start", "cron": "30 8 * * *", "tz": "Europe/Amsterdam", "name": "early"}], sha="b" * 40)
    clock.to(2026, 10, 31, 8, 1, 0)
    await s.tick()
    assert set(s.due) == {"sample/early"}
    [view] = s.view()
    assert (view["version"], view["tz"], view["next_at"]) == (1, "Europe/Amsterdam", "2026-11-01T07:30:00Z")
    env.programs.remove(env.programs.active("sample"))  # no active version: no schedule
    assert s.view() == [] and await s.tick() == []


async def test_a_state_file_that_is_not_an_object_is_empty_and_a_lost_run_shows_what_was_kept(env):
    deploy(env, [{"function": "start", "cron": "* * * * *"}])
    write_json(env.programs.root.parent / "schedules.json", [1])
    clock = Clock(2026, 10, 31, 9, 0, 0)
    s = make(env, clock)
    assert s.view()[0]["last"] is None
    kept = {"run_id": "6f1c2d4e-0000-4000-8000-00000000000a", "at": "x", "status": "done", "code": None}
    write_json(env.programs.root.parent / "schedules.json", {"sample/start": {"last": kept}})
    assert s.view()[0]["last"] == kept


async def test_the_loop_wakes_just_past_each_minute_and_survives_a_failing_tick(env, caplog):
    clock = Clock(2026, 10, 31, 9, 0, 15)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(round(seconds, 2))
        if len(slept) == 3:
            raise asyncio.CancelledError

    s = make(env, clock, sleep=sleep)
    ticks = {"n": 0}

    async def tick() -> list[str]:
        ticks["n"] += 1
        if ticks["n"] == 1:
            raise RuntimeError("boom")
        return []

    s.tick = tick  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR, "rrotor.scheduler"), pytest.raises(asyncio.CancelledError):
        await s.run()
    assert slept == [45.05, 45.05, 45.05] and ticks["n"] == 2 and s.last == clock.now
    assert "starting scheduled runs failed" in caplog.text


async def test_stop_cancels_the_runs_being_started(env):
    deploy(
        env,
        [{"function": "start", "cron": "* * * * *"}],
        "import time\n\ndef start(ctx, inputs):\n    time.sleep(30)\n",
    )
    clock = Clock(2026, 10, 31, 9, 0, 0)
    s = make(env, clock)
    await s.tick()
    clock.to(2026, 10, 31, 9, 1, 0)
    await s.tick()
    await asyncio.sleep(0.2)
    clock.to(2026, 10, 31, 9, 2, 0)
    assert await s.tick() == []  # still being started (its first Result not back): skipped
    assert s.view()[0]["skipped"]["count"] == 1
    await s.stop()
    assert s.tasks == {}
