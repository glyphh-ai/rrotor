"""Runs: a call opens one, each continue resumes it, cancel and expiry end it; `end` always runs. The worker stays
up while a run waits (up to 15 minutes); if it is gone (rrotor restarted), a continue starts a new one that loads
again and reads its checkpoints back (docs/architecture.md, 5.4). Every call answers a Result, never an exception:
the errors raised here are about the call itself (no such model, inputs refused), never about the model."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from rrotor.ctx import RunState
from rrotord.errors import Conflict, NotFound, RrotorError, Unavailable
from rrotord.licence import Licence
from rrotord.manifest import check_inputs
from rrotord.models import Models, Version
from rrotord.parts import BASE_MOD, KNOWN, Parts
from rrotord.proxy import EgressProxy
from rrotord.runner.launch import Launcher, Spec
from rrotord.runner.record import WAITING, Records, Run, new_id, now
from rrotord.runner.worker import Worker
from rrotord.sandbox import bwrap
from rrotord.sandbox.probe import Layers

log = logging.getLogger("rrotor.runner")
GRACE = 15.0  # past a run's seconds before its worker is killed (the worker stops itself first)
END_WAIT = 35.0
WAIT_LIMIT = 15 * 60.0

Notify = Callable[[Run], Awaitable[None]]


def failed(step: str, code: str, reason: str) -> dict[str, Any]:
    return {"status": "failed", "data": {"reason": reason, "code": code}, "step": step, "prompt": None}


class Runner:
    def __init__(
        self,
        *,
        models: Models,
        records: Records,
        proxy: EgressProxy,
        launcher: Launcher,
        sdk: Path,
        layers: Layers,
        notify: Notify | None = None,
        wait_limit: float = WAIT_LIMIT,
        parts: Parts | None = None,
        licence: Licence | None = None,
    ) -> None:
        self.models, self.records, self.proxy, self.launcher = models, records, proxy, launcher
        self.sdk, self.layers, self.wait_limit = sdk, layers, wait_limit
        self.parts, self.licence = parts, licence
        self.notify = notify
        self.workers: dict[str, Worker] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, run_id: str) -> asyncio.Lock:
        return self._locks.setdefault(run_id, asyncio.Lock())

    def get(self, run_id: str) -> Run:
        run = self.records.load(run_id)
        if run is None:
            raise NotFound("There is no such run.", code="E_RUN_MISSING", run_id=run_id)
        return run

    async def call(
        self,
        *,
        model: str,
        function: str,
        inputs: dict[str, Any],
        version: int | None = None,
        caller: str = "claude",
        origin: str = "local",
        run_id: str | None = None,
        clearance: float = 1.0,
    ) -> Run:
        if not self.layers.ready:
            raise Unavailable(
                f"The sandbox is not ready, so no model runs: {self.layers.why()}", code="E_SANDBOX_UNAVAILABLE"
            )
        found = self.models.get(model, version)
        fn = found.manifest.functions.get(function)
        if fn is None:
            raise NotFound(f"{model} has no function {function}.", code="E_FUNCTION_MISSING")
        check_inputs(fn, inputs)
        run = Run(run_id or new_id(), model, found.version, found.commit_sha, function, inputs, caller, origin)
        run.clearance = clearance
        self.records.save(run)
        self.records.log(run.id, {"event": "called", "function": function, "caller": caller, "version": found.version})
        async with self._lock(run.id):
            refused = self._parts_refused(model, found)
            if refused:
                await self._finish(run, refused)
            else:
                await self._go(run, found, None)
        return run

    def _parts_refused(self, model: str, found: Version) -> dict[str, Any] | None:
        """Why the run cannot go: a part its manifest asks for is not installed, or not in the plan."""
        needed = found.manifest.parts()
        if not needed:
            return None
        _, missing = self.parts.resolve(needed) if self.parts else ([], needed)
        if missing:
            brings = sorted({KNOWN.get(name, name) for name in missing})
            reason = (
                f"{model} needs the part{'s' if len(missing) > 1 else ''} {', '.join(missing)}, not installed "
                f"(from the {', '.join(brings)} mod{'s' if len(brings) > 1 else ''})."
            )
            return failed("parts", "E_PART_MISSING", reason)
        for name in needed:
            installed = self.parts.get(name) if self.parts else None
            try:
                if installed is not None and self.licence is not None and installed.info.mod != BASE_MOD:
                    self.licence.require(installed.info.mod)
            except RrotorError as err:
                return failed("parts", err.code, err.message)
        return None

    async def resume(self, run_id: str, answer: Any) -> Run:
        async with self._lock(run_id):
            run = self.get(run_id)
            if run.status not in WAITING:
                raise Conflict("This run is not waiting for an answer.", code="E_RUN_STATE", run_status=run.status)
            state = RunState.from_json(run.state)
            state.record(run.pending or {}, answer)
            run.state, run.status, run.waiting_since = state.to_json(), "running", None
            self.records.save(run)
            self.records.log(run.id, {"event": "continued", "pending": run.pending})
            found = self.models.by_sha(run.model, run.commit_sha)
            worker = self.workers.get(run.id)
            if found is None:
                await self._finish(run, failed("deploy", "E_VERSION_MISSING", "This run's version is no longer here."))
            elif worker is not None and worker.alive:
                await worker.send("continue", answer=answer, state=run.state, seconds_left=self._left(run, found))
                await self._await_result(run, worker, found)
            else:
                await self._go(run, found, answer)
            return run

    async def cancel(self, run_id: str) -> Run:
        run = self.get(run_id)
        if run.final:
            raise Conflict("This run has ended.", code="E_RUN_STATE", run_status=run.status)
        worker = self.workers.get(run_id)
        if run.status == "running" and worker is not None:
            await worker.send("cancel")  # the call in progress answers `cancelled`, then ends
        async with self._lock(run_id):
            run = self.get(run_id)
            if not run.final:
                await self._end(
                    run, self.workers.get(run_id), self._stopped("cancelled", "cancel", "The run was cancelled.")
                )
            return run

    @staticmethod
    def _stopped(status: str, step: str, reason: str) -> dict[str, Any]:
        return {"status": status, "data": {"reason": reason}, "step": step, "prompt": None}

    def _left(self, run: Run, found: Version) -> float:
        return max(found.manifest.limits.seconds - run.seconds, 0.0)

    def _mounts(self, model: str, found: Version, socket: Path | None) -> bwrap.Mounts:
        manifest = found.manifest
        files = self.models.files(model)
        files.mkdir(parents=True, exist_ok=True)
        data = self.models.data(model) if manifest.data else None
        if data:
            data.mkdir(parents=True, exist_ok=True)
        needed = manifest.parts()
        part_mounts, _ = self.parts.resolve(needed) if self.parts else ([], [])
        pythonpath = self.parts.env(needed, bwrap.PARTS) if self.parts else []
        return bwrap.Mounts(found.src, found.venv, self.sdk, files, socket, data, tuple(part_mounts), tuple(pythonpath))

    async def maintain(self, found: Version, op: str) -> dict[str, Any]:
        """rrotor's own work in a version's sandbox (a re-encode on deploy): one call of `op`, its Result."""
        refused = self._parts_refused(found.manifest.name, found)
        if refused:
            return refused
        mounts = self._mounts(found.manifest.name, found, None)
        limits = bwrap.Limits(found.manifest.limits.memory_mb, found.manifest.limits.seconds + 5)
        try:
            worker = await Worker.start(self.launcher, lambda fd: Spec(mounts, limits, fd), lambda m: None)
        except OSError as err:
            return failed("sandbox", "E_SANDBOX", f"The sandbox did not start: {err}")
        try:
            await worker.send(
                "start",
                function=op,
                inputs={},
                approvals=[],
                state={},
                answer=None,
                seconds_left=float(found.manifest.limits.seconds),
                sandbox=self.launcher.sandbox(mounts),
                hosts=[],
                caller="rrotor",
                **self.launcher.paths(mounts),
            )
            message = await worker.next(found.manifest.limits.seconds + GRACE)
        finally:
            await worker.stop(grace=5.0)
        if message is None or message.get("op") != "result":
            return failed(op, "E_WORKER", "The model's process stopped before it answered.")
        return dict(message["result"])

    async def _go(self, run: Run, found: Version, answer: Any) -> None:
        manifest = found.manifest
        hosts = frozenset(manifest.hosts)
        socket = await self.proxy.open(run.id, hosts, lambda e: self.records.log(run.id, e)) if hosts else None
        mounts = self._mounts(run.model, found, socket)
        limits = bwrap.Limits(manifest.limits.memory_mb, manifest.limits.seconds + 5)
        try:
            worker = await Worker.start(
                self.launcher, lambda fd: Spec(mounts, limits, fd), lambda m: self._note(run, m)
            )
        except OSError as err:
            await self.proxy.close(run.id)
            await self._finish(run, failed("sandbox", "E_SANDBOX", f"The sandbox did not start: {err}"))
            return
        self.workers[run.id] = worker
        state = RunState.from_json(run.state).to_json()
        fn = manifest.functions[run.function]
        await worker.send(
            "start",
            function=run.function,
            inputs=run.inputs,
            approvals=fn.approvals,
            state=state,
            answer=answer,
            seconds_left=self._left(run, found),
            sandbox=self.launcher.sandbox(mounts),
            hosts=sorted(hosts),
            clearance=run.clearance,
            caller=run.caller,
            **self.launcher.paths(mounts),
        )
        await self._await_result(run, worker, found)

    async def _await_result(self, run: Run, worker: Worker, found: Version) -> None:
        began = time.monotonic()
        message = await worker.next(self._left(run, found) + GRACE)
        run.seconds += time.monotonic() - began
        if message is None or message.get("op") != "result":
            alive = worker.alive and not worker.closed  # a closed channel is a worker that stopped, reaped or not
            await worker.stop(grace=0 if alive else 5.0)
            reason = (
                "The run went past its seconds and was stopped."
                if alive
                else f"The model's process stopped (exit {worker.proc.returncode})."
            )
            await self._end(run, None, failed(run.function, "E_LIMIT_SECONDS" if alive else "E_WORKER", reason))
            return
        result = message["result"]
        if result.get("status") in WAITING:
            run.status, run.pending, run.result, run.waiting_since = (
                result["status"],
                message.get("pending"),
                result,
                now(),
            )
            self.records.save(run)
            self.records.log(run.id, {"event": "waiting", "status": result["status"], "step": result.get("step")})
            await self._tell(run)
            return
        await self._end(run, worker, result)

    async def _end(self, run: Run, worker: Worker | None, result: dict[str, Any]) -> None:
        """`end` runs in the worker (if it is there), the worker goes, and the run has its final Result."""
        if worker is not None and worker.alive and await worker.send("end", reason=result["status"]):
            ended = await worker.next(END_WAIT)
            if ended and ended.get("result"):
                self.records.log(run.id, {"event": "end failed", "result": ended["result"]})
        if worker is not None:
            await worker.stop()
        await self._finish(run, result)

    async def _finish(self, run: Run, result: dict[str, Any]) -> None:
        self.workers.pop(run.id, None)
        await self.proxy.close(run.id)
        run.status, run.result, run.pending, run.ended_at, run.waiting_since = (
            result["status"],
            result,
            None,
            now(),
            None,
        )
        self.records.save(run)
        self.records.log(run.id, {"event": "ended", "status": run.status, "seconds": round(run.seconds, 3)})
        await self._tell(run)

    async def _tell(self, run: Run) -> None:
        if self.notify is not None:
            await self.notify(run)

    def _note(self, run: Run, message: dict[str, Any]) -> None:
        if message.get("op") == "checkpoint":
            state = RunState.from_json(run.state)
            state.checkpoints[str(message.get("key"))] = message.get("value")
            run.state = state.to_json()
            self.records.save(run)
        elif message.get("op") == "log":
            self.records.log(run.id, {"message": message.get("message", ""), "detail": message.get("detail") or {}})
        else:
            self.records.log(run.id, {k: v for k, v in message.items() if k != "op"})

    async def expire(self, at: dt.datetime | None = None) -> list[str]:
        """Waiting runs past 15 minutes end, `expired`; `end` runs."""
        at = at or dt.datetime.now(dt.UTC)
        gone = []
        for run in self.records.all():
            if (
                run.status in WAITING
                and run.waiting_since
                and (at - dt.datetime.fromisoformat(run.waiting_since)).total_seconds() >= self.wait_limit
            ):
                async with self._lock(run.id):
                    fresh = self.get(run.id)
                    if fresh.status in WAITING:
                        result = self._stopped("expired", "wait", "No answer came within 15 minutes.")
                        await self._end(fresh, self.workers.get(run.id), result)
                        gone.append(run.id)
        return gone

    async def recover(self) -> list[str]:
        """At start: runs that were running when rrotor stopped have failed; waiting ones can still be continued."""
        lost = []
        for run in self.records.all():
            if run.status == "running":
                await self._finish(run, failed(run.function, "E_RESTARTED", "rrotor restarted during the run."))
                lost.append(run.id)
        return lost

    def approvals(self) -> list[dict[str, Any]]:
        return [
            {
                "run_id": r.id,
                "model": r.model,
                "function": r.function,
                "origin": r.origin,
                "step": (r.result or {}).get("step"),
                "prompt": (r.result or {}).get("prompt"),
                "asked_at": r.waiting_since,
            }
            for r in self.records.all()
            if r.status == "needs_approval"
        ]

    async def shutdown(self) -> None:
        for worker in list(self.workers.values()):
            await worker.stop(grace=1)
        self.workers.clear()
