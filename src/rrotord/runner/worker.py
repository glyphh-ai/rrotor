"""One run's worker process, as the runner sees it: its channel (a socket pair, the worker's end as an inherited
descriptor), its output kept in the run's log, its messages in order."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
from collections.abc import Awaitable, Callable
from typing import Any

from rrotord.runner.launch import Launcher, Spec

Note = Callable[[dict[str, Any]], None]
Serve = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]  # a program's ctx.cli: the CLI's command, its Result
OUTPUT_LINES = 2000  # per stream, per worker: a chatty program is cut short in its log, never in its run


async def _no_cli(_: dict[str, Any]) -> dict[str, Any]:
    return {"status": "failed", "data": {"code": "E_CLI_MISSING", "reason": "This run runs no CLI."}}


class Worker:
    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        note: Note,
        serve: Serve = _no_cli,
    ) -> None:
        self.proc, self.reader, self.writer, self.note, self.serve = proc, reader, writer, note, serve
        self.inbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.closed = False  # the worker's end of the channel is gone
        self.tasks = [asyncio.create_task(self._read())]
        for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            if stream is not None:
                self.tasks.append(asyncio.create_task(self._output(name, stream)))

    @classmethod
    async def start(
        cls, launcher: Launcher, spec_for: Callable[[int], Spec], note: Note, serve: Serve = _no_cli
    ) -> Worker:
        parent, child = socket.socketpair()
        try:
            proc = await launcher.spawn(spec_for(child.fileno()))
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        reader, writer = await asyncio.open_connection(sock=parent, limit=16 * 1024 * 1024)
        return cls(proc, reader, writer, note, serve)

    async def _served(self, message: dict[str, Any]) -> None:
        """A program's ctx.cli, answered with the CLI command's Result (its id, so the worker knows which)."""
        try:  # shielded: a program stopped mid-command leaves the command to end as its own run does
            result = await asyncio.shield(self.serve(message))
        except Exception as err:  # noqa: BLE001  (the program is answered, never left waiting)
            result = {"status": "failed", "data": {"code": "E_CLI", "reason": str(err)}}
        await self.send("cli_result", id=message.get("id"), result=result)

    async def _read(self) -> None:
        try:
            while line := await self.reader.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    message = None
                if not isinstance(message, dict):
                    self.note({"worker": "unreadable message"})
                    continue
                if message.get("op") in ("checkpoint", "log"):
                    self.note(message)
                elif message.get("op") == "cli":
                    self.tasks.append(asyncio.create_task(self._served(message)))
                else:
                    await self.inbox.put(message)
        except (ConnectionError, OSError, ValueError):
            pass
        self.closed = True
        self.writer.close()  # a worker still sending to a channel nobody reads gets an error, not a wait forever
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
