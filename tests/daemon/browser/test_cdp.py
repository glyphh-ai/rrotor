"""CDP over a real pipe pair: a fake Chromium (a Python script) reads fd 3 and answers on fd 4."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest

from rrotord.browser.cdp import Cdp, CdpError
from tests.fakes import FakeProc

FAKE_CHROMIUM = r"""
import json, os, signal, sys
if "--ignore-term" in sys.argv:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
buf = b""
while True:
    chunk = os.read(3, 65536)
    if not chunk:
        break
    buf += chunk
    while b"\0" in buf:
        raw, buf = buf.split(b"\0", 1)
        msg = json.loads(raw)
        if msg["method"] == "fail":
            out = {"id": msg["id"], "error": {"message": "nope"}}
        elif msg["method"] == "event":
            out = {"method": "Page.loadEventFired", "params": {}}
            os.write(4, json.dumps(out).encode() + b"\0\0")
            out = {"id": msg["id"], "result": None}
        elif msg["method"] == "exit":
            sys.exit(0)
        else:
            out = {"id": msg["id"], "result": {"method": msg["method"], "session": msg.get("sessionId")}}
        os.write(4, json.dumps(out).encode() + b"\0")
"""


async def started(*extra: str) -> tuple[Cdp, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    cdp = Cdp(events.append)
    await cdp.start(sys.executable, ["-c", FAKE_CHROMIUM, *extra], {"PATH": os.environ.get("PATH", "")})
    return cdp, events


async def test_send_answers_by_id_and_events_go_to_the_handler():
    cdp, events = await started()
    assert await cdp.send("Browser.getVersion") == {"method": "Browser.getVersion", "session": None}
    assert (await cdp.send("Page.enable", {}, "s1"))["session"] == "s1"
    assert await cdp.send("event") == {}
    assert events == [{"method": "Page.loadEventFired", "params": {}}]
    with pytest.raises(CdpError, match="nope"):
        await cdp.send("fail")
    await cdp.stop()
    assert cdp.proc is not None and cdp.proc.returncode is not None


async def test_chromium_going_away_fails_what_waits():
    cdp, _ = await started()
    waiting = cdp.send("exit")
    with pytest.raises(CdpError, match="went away"):
        await waiting
    await asyncio.wait_for(cdp.closed.wait(), 5)
    with pytest.raises(CdpError, match="not running"):
        await cdp.send("x")
    await cdp.stop()


async def test_send_before_start_fails():
    with pytest.raises(CdpError, match="not running"):
        await Cdp(lambda m: None).send("x")


async def test_stop_before_start_does_nothing():
    await Cdp(lambda m: None).stop()


async def test_an_answer_to_nothing_asked_is_dropped():
    cdp = Cdp(lambda m: None)
    cdp.feed(b'{"id": 9, "result": {}}\0')
    future = asyncio.get_running_loop().create_future()
    future.set_result({})
    cdp.pending[1] = future
    cdp.dispatch({"id": 1, "result": {"late": True}})
    assert future.result() == {}


async def test_stop_kills_a_chromium_that_will_not_go():
    cdp = Cdp(lambda m: None)
    proc = FakeProc()
    cdp.proc = proc  # type: ignore[assignment]
    await cdp.stop()
    assert proc.terminated and proc.killed


async def test_a_process_already_gone_is_not_signalled():
    cdp = Cdp(lambda m: None)

    class Gone(FakeProc):
        def terminate(self) -> None:
            raise ProcessLookupError

        def kill(self) -> None:
            raise ProcessLookupError

    proc = Gone()
    cdp.proc = proc  # type: ignore[assignment]
    await cdp.stop()
    assert proc.waits == 2


async def test_a_stopped_process_is_left_alone():
    cdp = Cdp(lambda m: None)
    proc = FakeProc()
    proc.returncode = 0
    cdp.proc = proc  # type: ignore[assignment]
    await cdp.stop()
    assert proc.waits == 0


async def test_reading_to_the_end_leaves_answered_futures_alone():
    cdp = Cdp(lambda m: None)
    done = asyncio.get_running_loop().create_future()
    done.set_result({"ok": True})
    cdp.pending[1] = done
    reader = asyncio.StreamReader()
    reader.feed_eof()
    await cdp._read(reader)
    assert cdp.closed.is_set() and cdp.pending == {} and done.result() == {"ok": True}
