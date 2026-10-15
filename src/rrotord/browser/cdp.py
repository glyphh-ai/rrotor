"""Chromium's DevTools protocol over a private pipe (--remote-debugging-pipe: Chromium reads fd 3, writes fd 4), never
a port: nothing else in the machine can attach to a signed-in profile. Messages end with NUL."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Callable
from typing import Any

# Puts the two pipe ends at fds 3 and 4, then becomes Chromium: no preexec function in a threaded process.
DUP = "import os,sys;os.dup2(int(sys.argv[1]),3);os.dup2(int(sys.argv[2]),4);os.execv(sys.argv[3],sys.argv[3:])"

Event = Callable[[dict[str, Any]], None]


class CdpError(Exception):
    pass


class Cdp:
    def __init__(self, on_event: Event) -> None:
        self.on_event = on_event
        self.proc: asyncio.subprocess.Process | None = None
        self.seq = 0
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._writer: asyncio.WriteTransport | None = None
        self._buffer = b""
        self.closed = asyncio.Event()
        self.reading: asyncio.Task[None] | None = None

    async def start(self, chromium: str, args: list[str], env: dict[str, str]) -> None:
        cmd_r, cmd_w = os.pipe()
        ev_r, ev_w = os.pipe()
        try:
            self.proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                DUP,
                str(cmd_r),
                str(ev_w),
                chromium,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                pass_fds=(cmd_r, ev_w),
                env=env,
                start_new_session=True,
            )
        finally:
            os.close(cmd_r)
            os.close(ev_w)
        loop = asyncio.get_running_loop()
        self._writer, _ = await loop.connect_write_pipe(asyncio.Protocol, os.fdopen(cmd_w, "wb", buffering=0))
        reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(ev_r, "rb", buffering=0))
        self.reading = asyncio.create_task(self._read(reader))

    async def _read(self, reader: asyncio.StreamReader) -> None:
        try:
            while chunk := await reader.read(1 << 20):
                self.feed(chunk)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(CdpError("Chromium went away"))
            self.pending.clear()
            self.closed.set()

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while (end := self._buffer.find(b"\0")) >= 0:
            raw, self._buffer = self._buffer[:end], self._buffer[end + 1 :]
            if raw:
                self.dispatch(json.loads(raw))

    def dispatch(self, message: dict[str, Any]) -> None:
        if "id" in message:
            future = self.pending.pop(message["id"], None)
            if future is not None and not future.done():
                if "error" in message:
                    future.set_exception(CdpError(str(message["error"].get("message", "CDP error"))))
                else:
                    future.set_result(message.get("result") or {})
            return
        self.on_event(message)

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session: str | None = None,
    ) -> asyncio.Future[dict[str, Any]]:
        self.seq += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        if self._writer is None or self._writer.is_closing():
            future.set_exception(CdpError("Chromium is not running"))
            return future
        self.pending[self.seq] = future
        message = {
            "id": self.seq,
            "method": method,
            "params": params or {},
            **({"sessionId": session} if session else {}),
        }
        self._writer.write(json.dumps(message).encode() + b"\0")
        return future

    async def stop(self) -> None:
        if self._writer is not None:
            self._writer.close()
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()
                await self.proc.wait()
