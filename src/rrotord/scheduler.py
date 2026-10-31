"""The scheduler: the daemon calls each program's scheduled functions itself (docs/architecture.md, 5.6).

A program's manifest lists its `schedules`, each `{function, cron, tz?, input?, name?}` (rrotord/manifest.py,
rrotord/cron.py). Once a minute the scheduler looks at the active version of every program and starts each schedule
that came due since it last looked, as a normal run (`caller: schedule`) through the runner, so a run that asks for
an approval or an input waits for it like any other (and expires the same way).

- **No overlap:** a schedule whose previous run has not ended (still running, or waiting) is skipped that time; the
  skip is counted, logged and audited (`schedule.skipped`).
- **Nothing is replayed:** fires missed while the daemon was down (or before it was the head) are not made up; the
  first fire is the first one due after the scheduler started looking.
- **Paused and resumed** by `{program}/{name}` (the name is the function's unless the schedule names itself),
  kept in `/data/schedules.json` with each schedule's last run and skip, so a pause survives a restart and a new
  version.
- **In a cluster only the head fires** (or the node an unlicensed cluster serves from); a follower answers the list
  from its head and refuses to pause, like the vault. Schedules' state is the head's, node-local like the vault."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

from rrotord.audit import Audit, Who, acting
from rrotord.errors import NotFound, RrotorError
from rrotord.manifest import Schedule
from rrotord.programs import Programs, Version
from rrotord.runner.engine import Runner
from rrotord.runner.record import new_id
from rrotord.store import read_json, write_json

log = logging.getLogger("rrotor.scheduler")
SCHEDULE = Who({"type": "rrotor"}, None, "schedule")  # who a scheduled run is for, in the audit log
SLACK = 0.05  # seconds past the minute a tick wakes at

Sleep = Callable[[float], Awaitable[None]]


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def stamp(when: dt.datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


class Scheduler:
    def __init__(
        self,
        programs: Programs,
        runner: Runner,
        data: Path,
        *,
        clock: Callable[[], dt.datetime] = utcnow,
        leading: Callable[[], bool] = lambda: True,
        audit: Audit | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.programs, self.runner, self.path = programs, runner, data / "schedules.json"
        self.clock, self.leading, self.audit, self.sleep = clock, leading, audit, sleep
        self.due: dict[str, tuple[str, dt.datetime]] = {}  # each schedule's (cron and zone, next fire)
        self.last: dt.datetime | None = None  # when it last looked: what came due since then fires
        self.tasks: dict[str, asyncio.Task[None]] = {}

    def _audit(self, kind: str, **detail: Any) -> None:
        if self.audit is not None:
            self.audit.emit(kind, who=SCHEDULE, **detail)

    def _state(self) -> dict[str, dict[str, Any]]:
        found = read_json(self.path, {})
        return found if isinstance(found, dict) else {}

    def _update(self, key: str, **values: Any) -> None:
        state = self._state()
        state[key] = {**state.get(key, {}), **values}
        write_json(self.path, state)

    def entries(self) -> Iterator[tuple[str, Version, Schedule]]:
        """Each schedule of each program's active version, with its key `<program>/<name>`."""
        for name in self.programs.names():
            active = self.programs.active(name)
            if active is None:
                continue
            for schedule in active.manifest.schedules:
                yield f"{name}/{schedule.id}", active, schedule

    def _going(self, key: str, state: dict[str, Any]) -> str | None:
        """The run of this schedule that has not ended, if any."""
        task = self.tasks.get(key)
        last = (state.get("last") or {}).get("run_id")
        if task is not None and not task.done():
            return str(last)
        run = self.runner.records.load(last) if last else None
        return run.id if run is not None and not run.final else None

    async def tick(self) -> list[str]:
        """Start every schedule due since the last look; answers the keys started."""
        now = self.clock()
        since, self.last = self.last or now, now
        if not self.leading():
            self.due.clear()  # a follower fires nothing, and starts afresh if it is made head
            return []
        state = self._state()
        started: list[str] = []
        live: set[str] = set()
        for key, version, schedule in self.entries():
            live.add(key)
            shape = f"{schedule.cron} {schedule.tz}"
            known = self.due.get(key)
            if known is None or known[0] != shape:
                known = (shape, schedule.next_after(since))
            if now < known[1]:
                self.due[key] = known
                continue
            self.due[key] = (shape, schedule.next_after(now))
            mine = state.get(key, {})
            if mine.get("paused"):
                continue
            going = self._going(key, mine)
            if going is not None:
                skipped = dict(mine.get("skipped") or {})
                count = int(skipped.get("count") or 0) + 1
                self._update(key, skipped={"at": stamp(now), "run_id": going, "count": count})
                log.info("schedule skipped: its last run has not ended", extra={"schedule": key, "run_id": going})
                self._audit("schedule.skipped", schedule=key, program=version.program, run_id=going)
                continue
            self._start(key, version, schedule, now)
            started.append(key)
        for gone in set(self.due) - live:
            del self.due[gone]
        return started

    def _start(self, key: str, version: Version, schedule: Schedule, now: dt.datetime) -> None:
        run_id = new_id(self.runner.node())
        self._update(key, last={"run_id": run_id, "at": stamp(now), "status": "running", "code": None})
        self.tasks[key] = asyncio.create_task(self._fire(key, version, schedule, run_id, now))

    async def _fire(self, key: str, version: Version, schedule: Schedule, run_id: str, now: dt.datetime) -> None:
        with acting(SCHEDULE):
            try:
                run = await self.runner.call(
                    program=version.program,
                    function=schedule.function,
                    inputs=dict(schedule.input),
                    version=version.version,
                    caller="schedule",
                    run_id=run_id,
                )
            except RrotorError as err:
                last = {"run_id": None, "at": stamp(now), "status": "refused", "code": err.code}
                log.warning("a scheduled run was refused", extra={"schedule": key, "code": err.code})
            else:
                last = {"run_id": run.id, "at": stamp(now), "status": run.status, "code": None}
        self._update(key, last=last)

    def _last(self, mine: dict[str, Any]) -> dict[str, Any] | None:
        """The last run as it stands now (a waiting run may have ended since it was started)."""
        last = mine.get("last")
        if not last:
            return None
        run = self.runner.records.load(last["run_id"]) if last.get("run_id") else None
        if run is None:
            return dict(last)
        data = (run.result or {}).get("data")
        code = data.get("code") if run.status == "failed" and isinstance(data, dict) else None
        return {**last, "status": run.status, "code": code}

    def view(self) -> list[dict[str, Any]]:
        """Every schedule: where it is from, when it fires next (none while paused), its last run and skip."""
        now, state = self.clock(), self._state()
        out = []
        for key, version, schedule in self.entries():
            mine = state.get(key, {})
            paused = bool(mine.get("paused"))
            known = self.due.get(key)
            upcoming = known[1] if known is not None else schedule.next_after(now)
            out.append(
                {
                    "id": key,
                    "program": version.program,
                    "name": schedule.id,
                    "version": version.version,
                    "function": schedule.function,
                    "cron": schedule.cron,
                    "tz": schedule.tz,
                    "input": schedule.input,
                    "paused": paused,
                    "next_at": None if paused else stamp(upcoming),
                    "last": self._last(mine),
                    "skipped": mine.get("skipped"),
                }
            )
        return out

    def pause(self, program: str, name: str, paused: bool) -> dict[str, Any]:
        """A schedule paused or resumed, kept across restarts; an audit event."""
        key = f"{program}/{name}"
        if not any(k == key for k, _, _ in self.entries()):
            raise NotFound(f"{program} has no schedule {name}.", code="E_SCHEDULE_MISSING", schedule=key)
        self._update(key, paused=paused)
        self._audit("schedule.pause" if paused else "schedule.resume", schedule=key, program=program)
        return next(s for s in self.view() if s["id"] == key)

    async def run(self) -> None:
        """Look once a minute, just past it, from now on."""
        self.last = self.clock()
        while True:
            now = self.clock()
            await self.sleep(60 - now.second - now.microsecond / 1e6 + SLACK)
            try:
                await self.tick()
            except Exception:
                log.exception("starting scheduled runs failed")

    async def stop(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
