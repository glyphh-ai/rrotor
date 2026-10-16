"""The worker's life: start → lock down → import → load → the function (again after each continue) → end.

The runner sends `start`, then `continue` or `end`, and `cancel` while a function runs; the worker answers `result`
(with what a waiting run waits for), `checkpoint` and `log` as they happen, and `ended`. A function past its seconds
or cancelled stops where it is (a signal in the main thread) and `end` still runs."""

from __future__ import annotations

import importlib.util
import queue
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType, ModuleType
from typing import Any

from rrotor import ada
from rrotor._worker.channel import Channel
from rrotor._worker.forward import Forwarder
from rrotor._worker.landlock import Landlock, Rules
from rrotor.ctx import Ctx, RunState
from rrotor.http import Http
from rrotor.invoke import Outcome, crash, failure, invoke, lifecycle
from rrotor.result import Result

PROXY_URL = "http://127.0.0.1:3128"
_DONE = Result.done()


class Stop(BaseException):
    def __init__(self, status: str, code: str) -> None:
        super().__init__(status)
        self.status, self.code = status, code


def _on_alarm(_sig: int, _frame: FrameType | None) -> None:
    raise Stop("failed", "E_LIMIT_SECONDS")


def _on_cancel(_sig: int, _frame: FrameType | None) -> None:
    raise Stop("cancelled", "E_CANCELLED")


def import_model(folder: str) -> ModuleType:
    path = Path(folder) / "model.py"
    spec = importlib.util.spec_from_file_location("model", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path} cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules["model"] = module
    spec.loader.exec_module(module)
    return module


class Worker:
    def __init__(self, channel: Channel, landlock: Landlock | None = None) -> None:
        self.channel = channel
        self.landlock = landlock
        self.inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.running = threading.Event()
        self._main = threading.get_ident()  # a cancel is a signal to this thread: it interrupts a blocking call too

    def _read(self) -> None:
        while (message := self.channel.receive()) is not None:
            if message.get("op") == "cancel":
                if self.running.is_set():
                    signal.pthread_kill(self._main, signal.SIGUSR1)
                continue
            self.inbox.put(message)
        self.inbox.put(None)

    def _sink(self, op: str, data: dict[str, Any]) -> None:
        self.channel.send(op, **data)

    def _lock_down(self, start: dict[str, Any]) -> None:
        box = start.get("sandbox") or {}
        if box.get("landlock"):
            assert self.landlock is not None  # noqa: S101  (main() always passes one)
            rules = box["landlock"]
            self.landlock.restrict(
                Rules(tuple(rules["read"]), tuple(rules["write"]), tuple(rules.get("tcp_ports", ())))
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
        function = str(start["function"])
        state = RunState.from_json(start.get("state"))
        box = start.get("sandbox") or {}
        ctx = Ctx(
            inputs=dict(start.get("inputs") or {}),
            approvals=frozenset(start.get("approvals") or ()),
            state=state,
            files=Path(start.get("files", "/files")),
            http=Http(frozenset(start.get("hosts") or ()), PROXY_URL if box.get("proxy_socket") else None),
            answer=start.get("answer"),
            sink=self._sink,
            data=Path(start["data"]) if start.get("data") else None,
            home=Path(start.get("model", "/model")),
            clearance=float(start.get("clearance", 1.0)),
            caller=str(start.get("caller", "claude")),
        )
        try:
            model = import_model(str(start.get("model", "/model")))
        except BaseException as err:  # noqa: BLE001  (a model that cannot import is a failed run)
            self.channel.send("result", result=crash("load", err).to_json(), pending=None)
            return self._finish(None, ctx)
        seconds = float(start.get("seconds_left", 300))
        # rrotor's own re-encode runs before the model could open its store under the new spec: no load for it
        outcome = (
            Outcome(_DONE) if function == ada.REENCODE else self._timed(seconds, lambda: self._load(model, ctx), "load")
        )
        if outcome.result.status == "done":
            outcome = self._timed(seconds, lambda: self._call(model, function, ctx), function)
        while True:
            self.channel.send("result", result=outcome.result.to_json(), pending=outcome.pending)
            message = self.inbox.get()
            if message is None or message.get("op") != "continue":
                return self._finish(model, ctx)
            ctx._state = RunState.from_json(message.get("state"))
            ctx.answer = message.get("answer")
            left = float(message.get("seconds_left", seconds))
            outcome = self._timed(left, lambda: self._call(model, function, ctx), function)

    @staticmethod
    def _load(model: ModuleType, ctx: Ctx) -> Outcome:
        return Outcome(lifecycle("load", getattr(model, "load", None), ctx) or _DONE)

    @staticmethod
    def _call(model: ModuleType, function: str, ctx: Ctx) -> Outcome:
        if function == ada.REENCODE:  # rrotor's own maintenance on a deploy, never a model's function
            return invoke(function, lambda c: Result.done({"reencoded": ada.reencode(c)}), ctx)
        fn = getattr(model, function, None)
        if function.startswith("_") or function in ("load", "end") or not callable(fn):
            return Outcome(failure(function, "E_FUNCTION_MISSING", f"model.py has no function {function}."))
        return invoke(function, fn, ctx, ctx.inputs)

    def _finish(self, model: ModuleType | None, ctx: Ctx | None) -> int:
        """`end` always runs (when there is a model to end), then `ended`."""
        result = None
        if model is not None and ctx is not None:
            ending = getattr(model, "end", None)
            outcome = self._timed(30.0, lambda: Outcome(lifecycle("end", ending, ctx) or _DONE), "end")
            result = outcome.result.to_json() if outcome.result.status != "done" else None
        self.channel.send("ended", result=result)
        return 0
