"""Runs: a call opens one, each continue resumes it, cancel and expiry end it; `end` always runs. The worker stays
up while a run waits (up to 15 minutes); if it is gone (rrotor restarted), a continue starts a new one that loads
again and reads its checkpoints back (docs/architecture.md, 5.4). Every call answers a Result, never an exception:
the errors raised here are about the call itself (no such program, inputs refused), never about the program.

Every call, continue and cancel is an audit event with its outcome, and every run's end one with its final status
(rrotord/audit.py); while a run is handled its id, program, version and function are on every log line; with a drain,
its sandbox's start and its function are spans, and its end counted (rrotord/otel.py).

In a cluster (docs/specs/cluster.md, 3) a run's id names the node that holds it (`node`), and a follower's runs
mount the program's folders read-only (`readonly`).

A program that declares secrets gets their values from the vault (rrotord/vault.py) in its worker's start message,
and nowhere else: a declared one not set stops the run before `load`, `failed {step: secrets, missing: [...]}`, and
the values are masked in the run's log lines."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

from opentelemetry.trace import Span

from rrotor.ctx import RunState
from rrotord import log as logs
from rrotord.audit import Audit
from rrotord.errors import Conflict, NotFound, RrotorError, Unavailable
from rrotord.manifest import check_inputs
from rrotord.otel import Telemetry
from rrotord.parts import KNOWN, Parts
from rrotord.programs import Programs, Version
from rrotord.proxy import EgressProxy
from rrotord.runner.launch import Launcher, Spec
from rrotord.runner.record import WAITING, Records, Run, new_id, now
from rrotord.runner.worker import Worker
from rrotord.sandbox import bwrap
from rrotord.sandbox.probe import Layers
from rrotord.vault import Vault

log = logging.getLogger("rrotor.runner")
GRACE = 15.0  # past a run's seconds before its worker is killed (the worker stops itself first)
END_WAIT = 35.0
WAIT_LIMIT = 15 * 60.0
MASK = "[secret]"
MASK_MIN = 4  # a secret's value shorter than this is not masked (it would mask ordinary text)

Notify = Callable[[Run], Awaitable[None]]


def failed(step: str, code: str, reason: str) -> dict[str, Any]:
    return {"status": "failed", "data": {"reason": reason, "code": code}, "step": step, "prompt": None}


def masked(value: Any, hidden: list[str]) -> Any:
    """`value` with every secret's value in its strings replaced by [secret]."""
    if isinstance(value, str):
        for secret in hidden:
            value = value.replace(secret, MASK)
        return value
    if isinstance(value, dict):
        return {k: masked(v, hidden) for k, v in value.items()}
    if isinstance(value, list):
        return [masked(v, hidden) for v in value]
    return value


def about(run: Run) -> dict[str, Any]:
    """A run as its log lines and audit events name it."""
    return {"run_id": run.id, "program": run.program, "version": run.version, "function": run.function}


def outcome(run: Run) -> dict[str, Any]:
    """How a call ended, for its audit event: the status, and a failure's code (never the Result's data)."""
    data = (run.result or {}).get("data")
    code = data.get("code") if run.status == "failed" and isinstance(data, dict) else None
    return {"status": run.status, "code": code}


class Runner:
    def __init__(
        self,
        *,
        programs: Programs,
        records: Records,
        proxy: EgressProxy,
        launcher: Launcher,
        sdk: Path,
        layers: Layers,
        notify: Notify | None = None,
        wait_limit: float = WAIT_LIMIT,
        parts: Parts | None = None,
        audit: Audit | None = None,
        telemetry: Telemetry | None = None,
        node: Callable[[], str | None] = lambda: None,
        readonly: Callable[[], bool] = lambda: False,
        vault: Vault | None = None,
    ) -> None:
        self.node, self.readonly, self.vault = node, readonly, vault
        self.hidden: dict[str, list[str]] = {}  # each run's secret values, masked in its log lines
        self.programs, self.records, self.proxy, self.launcher = programs, records, proxy, launcher
        self.sdk, self.layers, self.wait_limit = sdk, layers, wait_limit
        self.parts, self.audit, self.telemetry = parts, audit, telemetry
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
        program: str,
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
            run = await self._call(program, function, inputs, version, caller, origin, run_id, clearance)
        except RrotorError as err:
            asked = {"program": program, "version": version, "function": function, "run_id": run_id}
            self._audit("run.call", **asked, outcome={"status": "refused", "code": err.code})
            raise
        self._audit("run.call", **about(run), outcome=outcome(run), seconds=round(run.seconds, 3))
        return run

    async def _call(
        self,
        program: str,
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
                f"The sandbox is not ready, so no program runs: {self.layers.why()}", code="E_SANDBOX_UNAVAILABLE"
            )
        found = self.programs.get(program, version)
        fn = found.manifest.functions.get(function)
        if fn is None:
            raise NotFound(f"{program} has no function {function}.", code="E_FUNCTION_MISSING")
        check_inputs(fn, inputs)
        run = Run(
            run_id or new_id(self.node()), program, found.version, found.commit_sha, function, inputs, caller, origin
        )
        run.clearance = clearance
        self.records.save(run)
        self.records.log(run.id, {"event": "called", "function": function, "caller": caller, "version": found.version})
        with logs.bind(**about(run)):
            async with self._lock(run.id):
                refused = self._parts_refused(program, found)
                if refused:
                    await self._finish(run, refused)
                else:
                    await self._go(run, found, None)
        return run

    def _parts_refused(self, program: str, found: Version) -> dict[str, Any] | None:
        """Why the run cannot go: a part its manifest asks for is not installed (a licence never refuses a run)."""
        needed = found.manifest.parts()
        if not needed:
            return None
        _, missing = self.parts.resolve(needed) if self.parts else ([], needed)
        if missing:
            brings = sorted({KNOWN.get(name, name) for name in missing})
            reason = (
                f"{program} needs the part{'s' if len(missing) > 1 else ''} {', '.join(missing)}, not installed "
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
                found = self.programs.by_sha(run.program, run.commit_sha)
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

    def _mounts(self, program: str, found: Version, socket: Path | None) -> bwrap.Mounts:
        manifest = found.manifest
        files = self.programs.files(program)
        files.mkdir(parents=True, exist_ok=True)
        data = self.programs.data(program) if manifest.data else None
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

    def _secrets(self, run: Run, found: Version) -> tuple[dict[str, str], dict[str, Any] | None]:
        """The values of the secrets the version declares, or why the run cannot go: one not set, or one the
        rrotor's key does not open."""
        declared = found.manifest.secrets
        if not declared:
            return {}, None
        try:
            values, missing = self.vault.values(run.program, declared) if self.vault else ({}, list(declared))
        except RrotorError as err:
            return {}, failed("secrets", err.code, err.message)
        if not missing:
            return values, None
        many = len(missing) > 1
        reason = (
            f"{run.program} needs the secret{'s' if many else ''} {', '.join(missing)}, not set on this rrotor: "
            f"set {'them' if many else 'it'} with PUT /v1/programs/{run.program}/secrets/<name> or secret_set."
        )
        refused = failed("secrets", "E_SECRET_MISSING", reason)
        refused["data"]["missing"] = missing
        return {}, refused

    async def _go(self, run: Run, found: Version, answer: Any) -> None:
        manifest = found.manifest
        values, refused = self._secrets(run, found)
        if refused is not None:
            await self._finish(run, refused)
            return
        hidden = sorted((v for v in values.values() if len(v) >= MASK_MIN), key=len, reverse=True)
        if hidden:
            self.hidden[run.id] = hidden
        hosts = frozenset(manifest.hosts)
        socket = await self.proxy.open(run.id, hosts, lambda e: self.records.log(run.id, e)) if hosts else None
        mounts = self._mounts(run.program, found, socket)
        limits = bwrap.Limits(manifest.limits.memory_mb, manifest.limits.seconds + 5)
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
            readonly=mounts.readonly,
            secrets={"declared": manifest.secrets, "values": values},
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
                else f"The program's process stopped (exit {worker.proc.returncode})."
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
        self.hidden.pop(run.id, None)
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
            self.telemetry.ended(run.program, run.function, run.status, run.seconds)
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
        else:
            entry = (
                {"message": message.get("message", ""), "detail": message.get("detail") or {}}
                if message.get("op") == "log"
                else {k: v for k, v in message.items() if k != "op"}
            )
            hidden = self.hidden.get(run.id)
            self.records.log(run.id, masked(entry, hidden) if hidden else entry)

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
                "program": r.program,
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
                or not self.programs.get(run.program, run.version).manifest.writes(run.function)
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
