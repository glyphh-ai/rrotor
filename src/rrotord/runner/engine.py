"""Runs: a call opens one, each continue resumes it, cancel and expiry end it; `end` always runs. The worker stays
up while a run waits (up to 15 minutes); if it is gone (rrotor restarted), a continue starts a new one that loads
again and reads its checkpoints back (docs/architecture.md, 5.4). Every call answers a Result, never an exception:
the errors raised here are about the call itself (no such model, inputs refused), never about the model.

Every call, continue and cancel is an audit event with its outcome, and every run's end one with its final status
(rrotord/audit.py); while a run is handled its id, model, version and function are on every log line; with a drain,
its sandbox's start and its function are spans, and its end counted (rrotord/otel.py).

In a cluster (docs/specs/cluster.md, 3) a run's id names the node that holds it (`node`); a follower's runs mount
the model's folders read-only and open its Ada store read-only (`readonly`); and on the head each run is bracketed
by `changes`, which installs the change capture in its store before and ships what changed after."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, Protocol

from opentelemetry.trace import Span

from rrotor.ctx import RunState
from rrotord import log as logs
from rrotord import precedents
from rrotord.audit import Audit
from rrotord.errors import Conflict, NotFound, RrotorError, Unavailable
from rrotord.licence import Licence
from rrotord.licence_key import COMMUNITY
from rrotord.manifest import check_inputs
from rrotord.models import Models, Version
from rrotord.otel import Telemetry
from rrotord.parts import KNOWN, Parts
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


class Changes(Protocol):
    """What a cluster's head does around the runs that may change what syncs (rrotord/cluster/replication.py)."""

    async def before(self, found: Version) -> None: ...

    async def after(self, model: str) -> None: ...


def failed(step: str, code: str, reason: str) -> dict[str, Any]:
    return {"status": "failed", "data": {"reason": reason, "code": code}, "step": step, "prompt": None}


def about(run: Run) -> dict[str, Any]:
    """A run as its log lines and audit events name it."""
    return {"run_id": run.id, "model": run.model, "version": run.version, "function": run.function}


def outcome(run: Run) -> dict[str, Any]:
    """How a call ended, for its audit event: the status, and a failure's code (never the Result's data)."""
    data = (run.result or {}).get("data")
    code = data.get("code") if run.status == "failed" and isinstance(data, dict) else None
    return {"status": run.status, "code": code}


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
        audit: Audit | None = None,
        telemetry: Telemetry | None = None,
        node: Callable[[], str | None] = lambda: None,
        readonly: Callable[[], bool] = lambda: False,
        changes: Changes | None = None,
    ) -> None:
        self.node, self.readonly, self.changes = node, readonly, changes
        self.models, self.records, self.proxy, self.launcher = models, records, proxy, launcher
        self.sdk, self.layers, self.wait_limit = sdk, layers, wait_limit
        self.parts, self.licence, self.audit, self.telemetry = parts, licence, audit, telemetry
        self.notify = notify
        self.workers: dict[str, Worker] = {}
        self.stopping: dict[str, dict[str, Any]] = {}  # runs stopped from outside, with the Result they end with
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, run_id: str) -> asyncio.Lock:
        return self._locks.setdefault(run_id, asyncio.Lock())

    def _audit(self, kind: str, **detail: Any) -> None:
        if self.audit is not None:
            self.audit.emit(kind, **detail)

    @contextlib.contextmanager
    def _span(self, name: str, run: Run) -> Iterator[Span | None]:
        if self.telemetry is None:
            yield None
            return
        with self.telemetry.span(name, {f"rrotor.{k}": v for k, v in about(run).items()}) as span:
            yield span

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
        """A run opened and its first Result; the call is an audit event either way (`refused` with its code when it
        never became a run)."""
        try:
            run = await self._call(model, function, inputs, version, caller, origin, run_id, clearance)
        except RrotorError as err:
            asked = {"model": model, "version": version, "function": function, "run_id": run_id}
            self._audit("run.call", **asked, outcome={"status": "refused", "code": err.code})
            raise
        self._audit("run.call", **about(run), outcome=outcome(run), seconds=round(run.seconds, 3))
        return run

    async def _call(
        self,
        model: str,
        function: str,
        inputs: dict[str, Any],
        version: int | None,
        caller: str,
        origin: str,
        run_id: str | None,
        clearance: float,
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
        run = Run(
            run_id or new_id(self.node()), model, found.version, found.commit_sha, function, inputs, caller, origin
        )
        run.clearance = clearance
        self.records.save(run)
        self.records.log(run.id, {"event": "called", "function": function, "caller": caller, "version": found.version})
        with logs.bind(**about(run)):
            async with self._lock(run.id):
                refused = self._parts_refused(model, found)
                if refused:
                    await self._finish(run, refused)
                else:
                    await self._go(run, found, None)
        return run

    def _parts_refused(self, model: str, found: Version) -> dict[str, Any] | None:
        """Why the run cannot go: a part its manifest asks for is not installed (a licence never refuses a run)."""
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
        return None

    async def resume(self, run_id: str, answer: Any) -> Run:
        """The answer a waiting run asked for, and its next Result; an audit event either way."""
        try:
            run = await self._resume(run_id, answer)
        except RrotorError as err:
            self._audit("run.continue", run_id=run_id, outcome={"status": "refused", "code": err.code})
            raise
        self._audit("run.continue", **about(run), outcome=outcome(run), seconds=round(run.seconds, 3))
        return run

    async def _resume(self, run_id: str, answer: Any) -> Run:
        async with self._lock(run_id):
            run = self.get(run_id)
            if run.status not in WAITING:
                raise Conflict("This run is not waiting for an answer.", code="E_RUN_STATE", run_status=run.status)
            with logs.bind(**about(run)):
                state = RunState.from_json(run.state)
                state.record(run.pending or {}, answer)
                run.state, run.status, run.waiting_since = state.to_json(), "running", None
                self.records.save(run)
                self.records.log(run.id, {"event": "continued", "pending": run.pending})
                found = self.models.by_sha(run.model, run.commit_sha)
                worker = self.workers.get(run.id)
                if found is None:
                    reason = "This run's version is no longer here."
                    await self._finish(run, failed("deploy", "E_VERSION_MISSING", reason))
                elif worker is not None and worker.alive:
                    await worker.send("continue", answer=answer, state=run.state, seconds_left=self._left(run, found))
                    await self._await_result(run, worker, found)
                else:
                    await self._go(run, found, answer)
            return run

    async def cancel(self, run_id: str) -> Run:
        """A run stopped, its `end` run; an audit event either way."""
        try:
            run = await self._cancel(run_id)
        except RrotorError as err:
            self._audit("run.cancel", run_id=run_id, outcome={"status": "refused", "code": err.code})
            raise
        self._audit("run.cancel", **about(run), outcome=outcome(run), seconds=round(run.seconds, 3))
        return run

    async def _cancel(self, run_id: str) -> Run:
        run = self.get(run_id)
        if run.final:
            raise Conflict("This run has ended.", code="E_RUN_STATE", run_status=run.status)
        worker = self.workers.get(run_id)
        if run.status == "running" and worker is not None:
            await worker.send("cancel")  # the call in progress answers `cancelled`, then ends
        async with self._lock(run_id):
            run = self.get(run_id)
            if not run.final:
                with logs.bind(**about(run)):
                    stopped = self._stopped("cancelled", "cancel", "The run was cancelled.")
                    await self._end(run, self.workers.get(run_id), stopped)
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
        return bwrap.Mounts(
            found.src,
            found.venv,
            self.sdk,
            files,
            socket,
            data,
            tuple(part_mounts),
            tuple(pythonpath),
            self.readonly(),
        )

    def _limits(self, found: Version) -> dict[str, Any]:
        """An Ada version's precedent cap: the licence key's `max_precedents` (Community's without one; null is
        unlimited), the plan's name for the caller's message, and, with a cap, `precedents_elsewhere`, the other
        models' total now (the node's cap is on its total)."""
        if "ada" not in found.manifest.needs:
            return {}
        plan = self.licence.limits() if self.licence is not None else COMMUNITY
        name = self.licence.plan_name if self.licence is not None else "Community"
        out: dict[str, Any] = {"precedents": plan.max_precedents, "plan": name}
        if plan.max_precedents is not None:
            out["precedents_elsewhere"] = self._elsewhere(found.model)
        return out

    def _elsewhere(self, model: str) -> int:
        """The precedents the runtime's other Ada models hold now: the plan's cap is on the total, and the model
        counts its own. A store that does not read counts none (logged)."""
        try:
            return precedents.total(self.models, skip=model)
        except sqlite3.Error as err:
            log.warning("a precedent store did not read", extra={"model": model, "reason": str(err)})
            return 0

    async def maintain(self, found: Version, op: str) -> dict[str, Any]:
        """rrotor's own work in a version's sandbox (a re-encode on deploy): one call of `op`, its Result."""
        refused = self._parts_refused(found.manifest.name, found)
        if refused:
            return refused
        mounts = self._mounts(found.manifest.name, found, None)
        limits = bwrap.Limits(found.manifest.limits.memory_mb, found.manifest.limits.seconds + 5)
        await self._before(found)
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
            await self._after(found.model)
        if message is None or message.get("op") != "result":
            return failed(op, "E_WORKER", "The model's process stopped before it answered.")
        return dict(message["result"])

    async def _go(self, run: Run, found: Version, answer: Any) -> None:
        manifest = found.manifest
        hosts = frozenset(manifest.hosts)
        socket = await self.proxy.open(run.id, hosts, lambda e: self.records.log(run.id, e)) if hosts else None
        mounts = self._mounts(run.model, found, socket)
        limits = bwrap.Limits(manifest.limits.memory_mb, manifest.limits.seconds + 5)
        await self._before(found)
        try:
            with self._span("rrotor.sandbox.start", run):
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
            limits=self._limits(found),
            readonly=mounts.readonly,
            **self.launcher.paths(mounts),
        )
        await self._await_result(run, worker, found)

    async def _await_result(self, run: Run, worker: Worker, found: Version) -> None:
        began = time.monotonic()
        with self._span("rrotor.function", run) as span:
            message = await worker.next(self._left(run, found) + GRACE)
            if span is not None and self.telemetry is not None:
                result = message.get("result") if message is not None else None
                span.set_attribute("rrotor.status", str((result or {}).get("status", "stopped")))
                self.telemetry.payload(span, inputs=run.inputs, data=(result or {}).get("data"))
        run.seconds += time.monotonic() - began
        if message is None or message.get("op") != "result":
            alive = worker.alive and not worker.closed  # a closed channel is a worker that stopped, reaped or not
            await worker.stop(grace=0 if alive else 5.0)
            reason = (
                "The run went past its seconds and was stopped."
                if alive
                else f"The model's process stopped (exit {worker.proc.returncode})."
            )
            stopped = self.stopping.pop(run.id, None)
            await self._end(
                run, None, stopped or failed(run.function, "E_LIMIT_SECONDS" if alive else "E_WORKER", reason)
            )
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
            await self._after(run.model)
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

    async def _before(self, found: Version) -> None:
        if self.changes is not None:
            await self.changes.before(found)

    async def _after(self, model: str) -> None:
        if self.changes is not None:
            await self.changes.after(model)

    async def _finish(self, run: Run, result: dict[str, Any]) -> None:
        self.workers.pop(run.id, None)
        await self._after(run.model)
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
        payload = {"inputs": run.inputs, "data": result.get("data")}
        self._audit("run.end", **about(run), outcome=outcome(run), seconds=round(run.seconds, 3), payload=payload)
        if self.telemetry is not None:
            self.telemetry.ended(run.model, run.function, run.status, run.seconds)
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

    async def stop_writing(self, code: str, reason: str) -> list[str]:
        """The runs in flight whose function writes, stopped, each to end `failed` with `code` (a cluster's head
        that was deposed: what they write would be no one's). Answers their ids."""
        stopped = []
        for run_id, worker in list(self.workers.items()):
            run = self.records.load(run_id)
            if (
                run is None
                or run.status != "running"
                or not self.models.get(run.model, run.version).manifest.writes(run.function)
            ):
                continue
            self.stopping[run_id] = failed(run.function, code, reason)
            await worker.stop(grace=0)
            stopped.append(run_id)
        return stopped

    async def shutdown(self) -> None:
        for worker in list(self.workers.values()):
            await worker.stop(grace=1)
        self.workers.clear()
