"""The worker's life: start → lock down → import → load → the function (again after each continue) → end.

The runner sends `start`, then `continue` or `end`, and `cancel` while a function runs; the worker answers `result`
(with what a waiting run waits for), `checkpoint` and `log` as they happen, and `ended`. A function past its seconds
or cancelled stops where it is (a signal in the main thread) and `end` still runs.

A CLI's run (`cli` in `start`) imports no program: once locked down, the worker runs its one command
(rrotor/_worker/command.py), timed and cancellable the same way, answers its `result` and, at `end`, `ended`. A CLI's
sign-in (`login` in its `cli`) keeps its command running on a pseudo-terminal across the person's wait
(rrotor/_worker/login.py): `needs_input` with the address it shows, each `continue` typed into it."""

from __future__ import annotations

import functools
import importlib.util
import os
import queue
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType, ModuleType
from typing import Any

from rrotor._worker import command, login
from rrotor._worker.channel import Channel
from rrotor._worker.forward import Forwarder
from rrotor._worker.landlock import Landlock, Rules
from rrotor.ctx import Ctx, RunState, Secrets
from rrotor.graph import Graph
from rrotor.http import Http
from rrotor.invoke import Outcome, crash, failure, invoke, lifecycle
from rrotor.result import Result

PROXY_URL = "http://127.0.0.1:3128"
DATABASE = "db.sqlite3"  # ctx.db, in the program's data folder
SDKS = ("/usr/local/lib/rrotor-sdk",)  # image/sdk.lock: importable by every program, as rrotor is
_DONE = Result.done()


class Stop(BaseException):
    def __init__(self, status: str, code: str) -> None:
        super().__init__(status)
        self.status, self.code = status, code


def _on_alarm(_sig: int, _frame: FrameType | None) -> None:
    raise Stop("failed", "E_LIMIT_SECONDS")


def _on_cancel(_sig: int, _frame: FrameType | None) -> None:
    raise Stop("cancelled", "E_CANCELLED")


def import_program(folder: str) -> ModuleType:
    path = Path(folder) / "program.py"
    spec = importlib.util.spec_from_file_location("program", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path} cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules["program"] = module
    spec.loader.exec_module(module)
    return module


class Worker:
    def __init__(self, channel: Channel, landlock: Landlock | None = None) -> None:
        self.channel = channel
        self.landlock = landlock
        self.inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.replies: queue.Queue[dict[str, Any] | None] = queue.Queue()  # what rrotor answered ctx.cli, ctx.graph
        self.asked = 0
        self.running = threading.Event()
        self._main = threading.get_ident()  # a cancel is a signal to this thread: it interrupts a blocking call too

    def _read(self) -> None:
        while (message := self.channel.receive()) is not None:
            if message.get("op") == "cancel":
                if self.running.is_set():
                    signal.pthread_kill(self._main, signal.SIGUSR1)
                continue
            if message.get("op") in ("cli_result", "graph_result"):
                self.replies.put(message)
                continue
            self.inbox.put(message)
        self.replies.put(None)
        self.inbox.put(None)

    def _asked(self, op: str, ask: dict[str, Any], code: str) -> dict[str, Any]:
        """What a program asked rrotor for over its channel, and rrotor's answer (a failure with `code` when rrotor
        went away meanwhile)."""
        self.asked += 1
        self.channel.send(op, id=self.asked, **ask)
        while (reply := self.replies.get()) is not None:
            if reply.get("id") == self.asked:
                result: dict[str, Any] = reply.get("result") or {}
                return result
        return {"status": "failed", "data": {"code": code, "reason": "rrotor went away during the call."}}

    def _cli(self, ask: dict[str, Any]) -> dict[str, Any]:
        """A CLI's command a program asked for (ctx.cli), run by rrotor in that CLI's sandbox: its Result."""
        return self._asked("cli", ask, "E_CLI")

    def _graph(self, ask: dict[str, Any]) -> dict[str, Any]:
        """A call of the rrotor's graph a program made (ctx.graph), done by rrotor: its Result."""
        return self._asked("graph", ask, "E_GRAPH")

    def _sink(self, op: str, data: dict[str, Any]) -> None:
        self.channel.send(op, **data)

    def _lock_down(self, start: dict[str, Any]) -> None:
        box = start.get("sandbox") or {}
        if box.get("landlock"):
            assert self.landlock is not None  # noqa: S101  (main() always passes one)
            rules = box["landlock"]
            self.landlock.restrict(
                Rules(
                    tuple(rules["read"]),
                    tuple(rules["write"]),
                    tuple(rules.get("tcp_ports", ())),
                    bool(rules.get("exec_writes", True)),
                    bool(rules.get("net", True)),
                )
            )
        if box.get("proxy_socket"):
            Forwarder(box["proxy_socket"]).start()

    def _timed(self, seconds: float, fn: Callable[[], Outcome], step: str) -> Outcome:
        signal.setitimer(signal.ITIMER_REAL, max(seconds, 0.001))
        self.running.set()
        try:
            return fn()
        except Stop as stop:
            if stop.status == "cancelled":
                return Outcome(Result("cancelled", {"reason": "The run was cancelled.", "code": stop.code}, step=step))
            return Outcome(failure(step, stop.code, "The run went past its seconds."))
        finally:
            self.running.clear()
            signal.setitimer(signal.ITIMER_REAL, 0)

    def run(self) -> int:
        start = self.channel.receive()
        if start is None or start.get("op") != "start":
            return 2
        signal.signal(signal.SIGALRM, _on_alarm)
        signal.signal(signal.SIGUSR1, _on_cancel)
        try:
            self._lock_down(start)
        except OSError as err:
            self.channel.send("result", result=failure("sandbox", "E_SANDBOX", str(err)).to_json(), pending=None)
            return self._finish(None, None)
        threading.Thread(target=self._read, daemon=True).start()
        if start.get("cli") and start["cli"].get("login") is not None:  # a CLI's sign-in: kept running across waits
            return self._sign_in(dict(start["cli"]), float(start.get("seconds_left", 300)))
        if start.get("cli"):  # a CLI's run: its one command, no program
            spec = dict(start["cli"])
            outcome = self._timed(float(start.get("seconds_left", 300)), lambda: Outcome(command.run(spec)), "cli")
            self.channel.send("result", result=outcome.result.to_json(), pending=None)
            self.inbox.get()  # `end`, or the runner gone
            return self._finish(None, None)
        function = str(start["function"])
        state = RunState.from_json(start.get("state"))
        box = start.get("sandbox") or {}
        secrets = start.pop("secrets", None) or {}  # {declared, values}: the values kept in ctx alone
        ctx = Ctx(
            inputs=dict(start.get("inputs") or {}),
            approvals=frozenset(start.get("approvals") or ()),
            state=state,
            files=Path(start.get("files", "/files")),
            http=Http(frozenset(start.get("hosts") or ()), PROXY_URL if box.get("proxy_socket") else None),
            answer=start.get("answer"),
            sink=self._sink,
            data=Path(start["data"]) if start.get("data") else None,
            home=Path(start.get("program", "/program")),
            clearance=float(start.get("clearance", 1.0)),
            caller=str(start.get("caller", "claude")),
            readonly=bool(start.get("readonly")),
            secrets=Secrets(secrets.get("declared") or (), secrets.get("values") or {}),
            database=Path(start["data"]) / DATABASE if start.get("sqlite") and start.get("data") else None,
            clis=start.get("clis"),
            request=self._cli,
            graph=Graph(start.get("graph"), self._graph),
            folders=start.get("folders") or (),
        )
        for sdk in SDKS:  # the SDKs the image carries (glyphh), after the program's own venv
            if os.path.isdir(sdk) and sdk not in sys.path:
                sys.path.append(sdk)
        try:
            program = import_program(str(start.get("program", "/program")))
        except BaseException as err:  # noqa: BLE001  (a program that cannot import is a failed run)
            self.channel.send("result", result=crash("load", err).to_json(), pending=None)
            return self._finish(None, ctx)
        seconds = float(start.get("seconds_left", 300))
        outcome = self._timed(seconds, lambda: self._load(program, ctx), "load")
        if outcome.result.status == "done":
            outcome = self._timed(seconds, lambda: self._call(program, function, ctx), function)
        while True:
            self.channel.send("result", result=outcome.result.to_json(), pending=outcome.pending)
            message = self.inbox.get()
            if message is None or message.get("op") != "continue":
                return self._finish(program, ctx)
            ctx._state = RunState.from_json(message.get("state"))
            ctx.answer = message.get("answer")
            left = float(message.get("seconds_left", seconds))
            outcome = self._timed(left, lambda: self._call(program, function, ctx), function)

    def _sign_in(self, spec: dict[str, Any], seconds: float) -> int:
        """A CLI's sign-in (rrotor/_worker/login.py): its command started on a terminal, a `needs_input` with the
        address it shows, each `continue` typed into it, until it ends; stopped at `end`, a cancel or its seconds."""
        session = login.Session(spec)
        try:
            refused = session.start()
            asked = lambda: Outcome(session.until_asked())  # noqa: E731
            outcome = Outcome(refused) if refused else self._timed(seconds, asked, "cli")
            while True:
                waiting = outcome.result.waiting
                self.channel.send("result", result=outcome.result.to_json(), pending=login.PENDING if waiting else None)
                message = self.inbox.get()
                if not waiting or message is None or message.get("op") != "continue":
                    break
                left = float(message.get("seconds_left", seconds))
                outcome = self._timed(left, functools.partial(self._answered, session, message.get("answer")), "cli")
        finally:
            session.stop()
        return self._finish(None, None)

    @staticmethod
    def _answered(session: login.Session, answer: Any) -> Outcome:
        return Outcome(session.answer(answer))

    @staticmethod
    def _load(program: ModuleType, ctx: Ctx) -> Outcome:
        return Outcome(lifecycle("load", getattr(program, "load", None), ctx) or _DONE)

    @staticmethod
    def _call(program: ModuleType, function: str, ctx: Ctx) -> Outcome:
        fn = getattr(program, function, None)
        if function.startswith("_") or function in ("load", "end") or not callable(fn):
            return Outcome(failure(function, "E_FUNCTION_MISSING", f"program.py has no function {function}."))
        return invoke(function, fn, ctx, ctx.inputs)

    def _finish(self, program: ModuleType | None, ctx: Ctx | None) -> int:
        """`end` always runs (when there is a program to end), then `ended`."""
        result = None
        if program is not None and ctx is not None:
            ending = getattr(program, "end", None)
            outcome = self._timed(30.0, lambda: Outcome(lifecycle("end", ending, ctx) or _DONE), "end")
            result = outcome.result.to_json() if outcome.result.status != "done" else None
        if ctx is not None:
            ctx.close()
        self.channel.send("ended", result=result)
        return 0
