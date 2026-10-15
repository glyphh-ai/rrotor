"""The worker in this process: a socket pair for its channel, a program in a folder, the runner played by a thread."""

from __future__ import annotations

import contextlib
import json
import socket
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from rrotor._worker.channel import Channel
from rrotor._worker.loop import Worker, import_program

pytestmark = pytest.mark.usefixtures("restore_signals")

React = Callable[[dict[str, Any], "Runner"], None]


class Runner:
    """The runner's end of the channel: records what the worker says and answers with `react`."""

    def __init__(self, react: React | None = None) -> None:
        self.mine, self.theirs = socket.socketpair()
        self.reader, self.writer = self.mine.makefile("rb"), self.mine.makefile("wb")
        self.got: list[dict[str, Any]] = []
        self.react = react or (lambda message, runner: None)
        self.thread = threading.Thread(target=self._listen, daemon=True)

    def channel(self) -> Channel:
        return Channel(self.theirs.makefile("rb"), self.theirs.makefile("wb"))

    def send(self, op: str, **data: Any) -> None:
        self.writer.write(json.dumps({"op": op, **data}).encode() + b"\n")
        self.writer.flush()

    def _listen(self) -> None:
        while line := self.reader.readline():
            message = json.loads(line)
            self.got.append(message)
            self.react(message, self)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.theirs.shutdown(socket.SHUT_RDWR)
        self.theirs.close()
        self.thread.join(5)
        self.mine.close()

    def ops(self) -> list[str]:
        return [m["op"] for m in self.got]

    def results(self) -> list[dict[str, Any]]:
        return [m["result"] for m in self.got if m["op"] == "result"]


def end_after_result(message: dict[str, Any], runner: Runner) -> None:
    if message["op"] == "result":
        runner.send("end")


@pytest.fixture(autouse=True)
def forget_program() -> Iterator[None]:
    yield
    sys.modules.pop("program", None)


def write(folder: Path, source: str) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "program.py").write_text(source)
    return str(folder)


def run(start: dict[str, Any] | None, react: React | None = end_after_result, landlock: Any = None) -> Runner:
    runner = Runner(react)
    runner.thread.start()
    if start is not None:
        runner.send("start", **start)
    else:
        runner.send("hello")
    code = Worker(runner.channel(), landlock).run()
    runner.close()
    runner.code = code  # type: ignore[attr-defined]
    return runner


def start_for(folder: str, function: str = "start", **extra: Any) -> dict[str, Any]:
    return {"function": function, "program": folder, "files": folder, "inputs": {"a": 1}, **extra}


PROGRAM = """
from rrotor import Result

def load(ctx):
    ctx.log("loaded")

def start(ctx, inputs):
    ctx.checkpoint("c", lambda: 7)
    return Result.done({"inputs": inputs, "files": str(ctx.files)})

def end(ctx):
    ctx.log("ended")
"""


def test_a_message_other_than_start_stops_the_worker():
    assert run(None, None).code == 2  # type: ignore[attr-defined]


def test_a_run_loads_calls_and_ends(tmp_path):
    folder = write(tmp_path / "p", PROGRAM)
    runner = run(start_for(folder))
    assert runner.ops() == ["log", "checkpoint", "result", "log", "ended"]
    assert runner.results() == [
        {"status": "done", "data": {"inputs": {"a": 1}, "files": folder}, "step": None, "prompt": None}
    ]
    assert runner.got[-1] == {"op": "ended", "result": None}


def test_a_waiting_run_continues_with_its_answer_and_state(tmp_path):
    folder = write(
        tmp_path / "p",
        """
from rrotor import Result

def start(ctx, inputs):
    if ctx.ask_approval("write", "Write?"):
        return Result.done(ctx.answer)
    return Result.done("declined")
""",
    )

    def react(message: dict[str, Any], runner: Runner) -> None:
        if message["op"] == "result" and message["result"]["status"] == "needs_approval":
            assert message["pending"] == {"kind": "approval", "key": "write"}
            runner.send("continue", answer={"approved": True}, state={"approvals": {"write": True}}, seconds_left=10)
        elif message["op"] == "result":
            runner.send("end")

    runner = run(start_for(folder, approvals=["write"]), react)
    assert [r["status"] for r in runner.results()] == ["needs_approval", "done"]
    assert runner.results()[1]["data"] == {"approved": True}


def test_the_runner_going_away_ends_the_run(tmp_path):
    folder = write(tmp_path / "p", PROGRAM)

    def react(message: dict[str, Any], runner: Runner) -> None:
        if message["op"] == "result":
            runner.writer.close()
            runner.mine.shutdown(socket.SHUT_WR)

    assert run(start_for(folder), react).ops()[-1] == "ended"


def test_a_program_that_cannot_import_fails_at_load(tmp_path):
    folder = write(tmp_path / "p", "raise ImportError('nope')\n")
    runner = run(start_for(folder), None)
    assert runner.results()[0]["data"]["reason"] == "ImportError: nope"
    assert runner.got[-1] == {"op": "ended", "result": None}


def test_import_program_without_a_loader_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("importlib.util.spec_from_file_location", lambda *a: None)
    with pytest.raises(ImportError, match="cannot be loaded"):
        import_program(str(tmp_path))


def test_a_failed_load_skips_the_function(tmp_path):
    folder = write(
        tmp_path / "p",
        "def load(ctx):\n    raise RuntimeError('no')\n\ndef start(ctx, inputs):\n    raise AssertionError\n",
    )
    result = run(start_for(folder)).results()[0]
    assert (result["status"], result["step"]) == ("failed", "load")


@pytest.mark.parametrize("function", ["missing", "_private", "load", "end", "VALUE"])
def test_a_function_that_is_not_there_fails(tmp_path, function):
    folder = write(tmp_path / "p", "VALUE = 1\n\ndef _private(ctx, inputs):\n    pass\n")
    assert run(start_for(folder, function)).results()[0]["data"]["code"] == "E_FUNCTION_MISSING"


def test_a_failing_end_is_reported_in_ended(tmp_path):
    folder = write(
        tmp_path / "p",
        "from rrotor import Result\n\ndef start(ctx, inputs):\n    return Result.done()\n\n"
        "def end(ctx):\n    raise ValueError('end broke')\n",
    )
    ended = run(start_for(folder)).got[-1]
    assert ended["result"]["step"] == "end"


def test_a_function_past_its_seconds_is_stopped(tmp_path):
    folder = write(tmp_path / "p", "import time\n\ndef start(ctx, inputs):\n    time.sleep(10)\n")
    result = run(start_for(folder, seconds_left=0.2)).results()[0]
    assert (result["status"], result["data"]["code"]) == ("failed", "E_LIMIT_SECONDS")


def test_a_cancel_stops_the_function_in_progress(tmp_path):
    folder = write(
        tmp_path / "p",
        "import time\n\ndef start(ctx, inputs):\n    ctx.log('going')\n    time.sleep(10)\n",
    )

    def react(message: dict[str, Any], runner: Runner) -> None:
        if message["op"] == "log":
            runner.send("cancel")
        elif message["op"] == "result":
            runner.send("end")

    result = run(start_for(folder), react).results()[0]
    assert (result["status"], result["data"]["code"]) == ("cancelled", "E_CANCELLED")


def test_a_cancel_while_waiting_is_ignored(tmp_path):
    folder = write(
        tmp_path / "p", "from rrotor import Result\n\ndef start(ctx, inputs):\n    return Result.needs_input('x')\n"
    )

    def react(message: dict[str, Any], runner: Runner) -> None:
        if message["op"] == "result":
            runner.send("cancel")
            runner.send("end")

    runner = run(start_for(folder), react)
    assert runner.ops() == ["result", "ended"]


class Lock:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.rules: Any = None

    def restrict(self, rules: Any) -> int:
        if self.fail:
            raise OSError("no landlock here")
        self.rules = rules
        return 6


def test_the_worker_locks_itself_down_before_the_program(tmp_path):
    folder = write(tmp_path / "p", PROGRAM)
    lock = Lock()
    box = {"landlock": {"read": ["/usr"], "write": ["/files"], "tcp_ports": [3128]}}
    run(start_for(folder, sandbox=box), landlock=lock)
    assert lock.rules is not None
    assert (lock.rules.read, lock.rules.write, lock.rules.tcp_ports) == (("/usr",), ("/files",), (3128,))


def test_a_sandbox_that_cannot_lock_fails_the_run(tmp_path):
    folder = write(tmp_path / "p", PROGRAM)
    box: dict[str, Any] = {"landlock": {"read": [], "write": []}}
    runner = run(start_for(folder, sandbox=box), None, landlock=Lock(fail=True))
    assert runner.results()[0]["data"] == {"reason": "no landlock here", "code": "E_SANDBOX"}
    assert runner.ops() == ["result", "ended"]


def test_a_proxy_socket_opens_the_door_and_points_http_at_it(tmp_path, monkeypatch):
    opened = []

    class Door:
        def __init__(self, target: str) -> None:
            opened.append(target)

        def start(self) -> Door:
            return self

    monkeypatch.setattr("rrotor._worker.loop.Forwarder", Door)
    folder = write(
        tmp_path / "p",
        "from rrotor import Result\n\ndef start(ctx, inputs):\n    return Result.done(sorted(ctx.http.hosts))\n",
    )
    runner = run(start_for(folder, sandbox={"proxy_socket": "/run/p.sock"}, hosts=["a.example.com"]))
    assert opened == ["/run/p.sock"]
    assert runner.results()[0]["data"] == ["a.example.com"]
