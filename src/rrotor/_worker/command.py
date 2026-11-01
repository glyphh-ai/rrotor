"""A CLI's one command, run by the worker in its sandbox (docs/architecture.md, 5.7): the runner says what to run, with
which environment, in which working folder, with what on stdin. The worker keeps the head of stdout and stderr for
the Result (each capped) and passes the whole of both to its own output, which the runner keeps in the run's log
(line by line, long lines cut, a few MB at most)."""

from __future__ import annotations

import os
import selectors
import shutil
import subprocess
import sys
import tempfile
from typing import IO, Any

from rrotor.invoke import failure
from rrotor.result import Result

LOG_LINE = 2000  # the run's log cuts a line here anyway
LOG_MAX = 4 * 1024 * 1024  # per stream: past it, the log says the rest was not kept
CHUNK = 64 * 1024


def argv(spec: dict[str, Any]) -> list[str]:
    """The command line: the install's executable, or the interpreter that runs its script, then the arguments."""
    path, args = str(spec["path"]), [str(a) for a in spec.get("args") or []]
    match spec.get("run"):
        case "node":
            node = shutil.which("node", path=str((spec.get("env") or {}).get("PATH", os.defpath))) or "node"
            return [node, path, *args]
        case "python":
            return [sys.executable, path, *args]
    return [path, *args]


class Stream:
    """One of the command's outputs: its head for the Result, all of it (line by line) to the log."""

    def __init__(self, name: str, cap: int, out: IO[bytes]) -> None:
        self.name, self.cap, self.out = name, cap, out
        self.head, self.size, self.logged, self.partial = bytearray(), 0, 0, b""

    def take(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if len(self.head) < self.cap:
            self.head += chunk[: self.cap - len(self.head)]
        *lines, self.partial = (self.partial + chunk).split(b"\n")
        if len(self.partial) > LOG_LINE:
            lines, self.partial = [*lines, self.partial], b""
        for line in lines:
            self._log(line)

    def _log(self, line: bytes) -> None:
        if self.logged > LOG_MAX:
            return
        self.logged += len(line) + 1
        if self.logged > LOG_MAX:
            line = f"(more {self.name} not kept)".encode()
        self.out.write(line[:LOG_LINE] + b"\n")
        self.out.flush()

    def close(self) -> None:
        if self.partial:
            self._log(self.partial)
            self.partial = b""

    def text(self) -> str:
        return self.head.decode(errors="replace")


def run(spec: dict[str, Any], outputs: tuple[IO[bytes], IO[bytes]] | None = None) -> Result:
    """The command run to its end: `done` with its exit code, stdout and stderr (each its head, `truncated` when cut);
    `failed`, E_CLI_EXEC, when it cannot start. A signal that stops the run (its seconds, a cancel) stops the
    command too."""
    command = argv(spec)
    cap = int(spec.get("cap") or 64 * 1024)
    out, err = outputs or (sys.stdout.buffer, sys.stderr.buffer)
    streams = {"stdout": Stream("stdout", cap, out), "stderr": Stream("stderr", cap, err)}
    with tempfile.TemporaryFile() as stdin:
        stdin.write(str(spec.get("stdin") or "").encode())
        stdin.seek(0)
        try:
            proc = subprocess.Popen(
                command,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=spec.get("cwd"),
                env={str(k): str(v) for k, v in (spec.get("env") or {}).items()},
            )
        except OSError as error:
            return failure("cli", "E_CLI_EXEC", f"{command[0]} could not start: {error}")
        try:
            _pump(proc, streams)
            code = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    for stream in streams.values():
        stream.close()
    return Result.done(
        {
            "exit_code": code,
            "stdout": streams["stdout"].text(),
            "stderr": streams["stderr"].text(),
            "stdout_bytes": streams["stdout"].size,
            "stderr_bytes": streams["stderr"].size,
            "truncated": any(s.size > s.cap for s in streams.values()),
        }
    )


def _pump(proc: subprocess.Popen[bytes], streams: dict[str, Stream]) -> None:
    """Both outputs read until each ends."""
    with selectors.DefaultSelector() as selector:
        for name in ("stdout", "stderr"):
            pipe = getattr(proc, name)
            selector.register(pipe, selectors.EVENT_READ, streams[name])
        while selector.get_map():
            for key, _ in selector.select():
                chunk = os.read(key.fd, CHUNK)
                if chunk:
                    key.data.take(chunk)
                else:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()  # type: ignore[union-attr]
