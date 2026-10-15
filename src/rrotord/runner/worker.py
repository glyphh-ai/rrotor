"""One run's worker process, as the runner sees it: its channel (a socket pair, the worker's end as an inherited
descriptor), its output kept in the run's log, its messages in order."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
from collections.abc import Callable
from typing import Any

from rrotord.runner.launch import Launcher, Spec

Note = Callable[[dict[str, Any]], None]
OUTPUT_LINES = 2000  # per stream, per worker: a chatty program is cut short in its log, never in its run


class Worker:
    def __init__(
        self, proc: asyncio.subprocess.Process, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, note: Note
    ) -> None:
        self.proc, self.reader, self.writer, self.note = proc, reader, writer, note
        self.inbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.tasks = [asyncio.create_task(self._read())]
        for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            if stream is not None:
                self.tasks.append(asyncio.create_task(self._output(name, stream)))

    @classmethod
    async def start(cls, launcher: Launcher, spec_for: Callable[[int], Spec], note: Note) -> Worker:
        parent, child = socket.socketpair()
        try:
            proc = await launcher.spawn(spec_for(child.fileno()))
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        reader, writer = await asyncio.open_connection(sock=parent, limit=16 * 1024 * 1024)
        return cls(proc, reader, writer, note)

    async def _read(self) -> None:
        try:
            while line := await self.reader.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    self.note({"worker": "unreadable message"})
                    continue
                if message.get("op") in ("checkpoint", "log"):
                    self.note(message)
                else:
                    await self.inbox.put(message)
        except (ConnectionError, OSError, ValueError):
            pass
        await self.inbox.put(None)

    async def _output(self, name: str, stream: asyncio.StreamReader) -> None:
        count = 0
        while line := await stream.readline():
            count += 1
            if count <= OUTPUT_LINES:
                self.note({"stream": name, "line": line.decode(errors="replace").rstrip("\n")})
            elif count == OUTPUT_LINES + 1:
                self.note({"stream": name, "line": f"(more {name} not kept)"})

    async def send(self, op: str, **data: Any) -> bool:
        try:
            self.writer.write(json.dumps({"op": op, **data}, separators=(",", ":")).encode() + b"\n")
            await self.writer.drain()
        except (ConnectionError, OSError):
            return False
        return True

    async def next(self, timeout: float) -> dict[str, Any] | None:
        """The worker's next `result` or `ended`; None when it went away or took longer than `timeout`."""
        try:
            return await asyncio.wait_for(self.inbox.get(), timeout)
        except TimeoutError:
            return None

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None

    async def stop(self, grace: float = 5.0) -> None:
        """Gone, its whole process group with it."""
        with contextlib.suppress(Exception):
            self.writer.close()
        if self.alive:
            try:
                await asyncio.wait_for(self.proc.wait(), grace)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
                await self.proc.wait()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
