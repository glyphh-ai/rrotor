import asyncio
import socket
import sys
from typing import Any

import pytest

from rrotord.runner import worker as worker_mod
from rrotord.runner.launch import Spec
from rrotord.runner.worker import Worker
from tests.fakes import FakeProc


class Script:
    """A launcher whose worker is a short Python script writing to the channel's descriptor."""

    def __init__(self, code: str, fail: BaseException | None = None) -> None:
        self.code, self.fail = code, fail

    def sandbox(self, mounts: Any) -> None:
        return None

    def paths(self, mounts: Any) -> dict[str, str]:
        return {}

    async def spawn(self, spec: Spec) -> asyncio.subprocess.Process:
        if self.fail is not None:
            raise self.fail
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            f"import os, sys, time\nfd = {spec.channel_fd}\n{self.code}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            pass_fds=(spec.channel_fd,),
            start_new_session=True,
        )


async def start(code: str) -> tuple[Worker, list[dict[str, Any]]]:
    notes: list[dict[str, Any]] = []
    w = await Worker.start(Script(code), lambda fd: Spec(None, None, fd), notes.append)  # type: ignore[arg-type]
    return w, notes


async def test_messages_in_order_notes_aside():
    w, notes = await start(
        'os.write(fd, b\'not json\\n[1]\\n{"op":"log","message":"m"}\\n{"op":"result","n":1}\\n\')\n'
        "print('out'); sys.stderr.write('err\\n')"
    )
    assert await w.next(5) == {"op": "result", "n": 1}
    assert await w.next(5) is None
    await w.proc.wait()
    await w.stop()
    assert w.closed and not w.alive
    assert {"worker": "unreadable message"} in notes and notes.count({"worker": "unreadable message"}) == 2
    assert {"op": "log", "message": "m"} in notes
    assert {"stream": "stdout", "line": "out"} in notes and {"stream": "stderr", "line": "err"} in notes


async def test_a_chatty_worker_is_cut_short_in_its_log(monkeypatch):
    monkeypatch.setattr(worker_mod, "OUTPUT_LINES", 2)
    w, notes = await start("print('1\\n2\\n3\\n4')")
    await w.proc.wait()
    await w.next(5)
    await asyncio.sleep(0.05)
    await w.stop()
    lines = [n["line"] for n in notes if n.get("stream") == "stdout"]
    assert lines == ["1", "2", "(more stdout not kept)"]


async def test_send_reaches_the_worker():
    w, _ = await start(
        "import json\ndata = os.read(fd, 100)\n"
        "os.write(fd, json.dumps({'op': 'result', 'got': data.decode()}).encode() + b'\\n')"
    )
    assert await w.send("start", a=1)
    message = await w.next(5)
    assert message is not None and message["got"] == '{"op":"start","a":1}\n'
    await w.stop()


async def test_next_answers_none_after_its_time():
    w, _ = await start("time.sleep(5)")
    assert await w.next(0.05) is None
    await w.stop(grace=0)
    assert not w.alive


async def test_a_launcher_that_fails_closes_the_channel():
    with pytest.raises(OSError, match="no bwrap"):
        await Worker.start(Script("", fail=OSError("no bwrap")), lambda fd: Spec(None, None, fd), print)  # type: ignore[arg-type]


async def worker_on(proc: FakeProc) -> tuple[Worker, socket.socket]:
    mine, theirs = socket.socketpair()
    reader, writer = await asyncio.open_connection(sock=mine)
    return Worker(proc, reader, writer, lambda m: None), theirs  # type: ignore[arg-type]


async def test_send_answers_false_when_the_worker_is_gone():
    proc = FakeProc()
    w, theirs = await worker_on(proc)

    def broken(data: bytes) -> None:
        raise BrokenPipeError

    w.writer.write = broken  # type: ignore[method-assign,assignment]
    assert not await w.send("end")
    theirs.close()
    proc.returncode = 0
    await w.stop()


async def test_stop_kills_the_process_group_of_a_worker_that_will_not_go():
    proc = FakeProc()
    w, theirs = await worker_on(proc)
    await w.stop(grace=0.01)
    assert proc.waits == 2 and not w.alive
    theirs.close()


async def test_a_read_that_fails_ends_the_messages():
    proc = FakeProc()
    w, theirs = await worker_on(proc)
    sending = asyncio.ensure_future(asyncio.to_thread(theirs.sendall, b"x" * (17 * 1024 * 1024)))
    assert await w.next(5) is None and w.closed
    theirs.close()
    await asyncio.gather(sending, return_exceptions=True)
    proc.returncode = 0
    await w.stop()
