"""A CLI's sign-in from afar (docs/architecture.md, 5.7, signing in), run by the worker in its sandbox: the CLI's
sign-in command on a pseudo-terminal, kept running across the person's wait. What it prints is read all along (so a
spinner never blocks it), cleaned of terminal codes and kept to its tail; once it shows a sign-in address (and a
device code, for a device flow) and settles, the run answers `needs_input` with them. The answer program_continue
brings is typed into the command (then Enter), or, when it is the address the browser ended on (a loopback redirect
the phone could not open), requested from the CLI's own listener inside the sandbox. The run ends `done` when the
command exits or its `signed_in` file appears (the command is then stopped), and asks again when it shows a new
address or goes quiet while still running."""

from __future__ import annotations

import contextlib
import fcntl
import http.client
import os
import re
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlsplit

from rrotor._worker.command import argv
from rrotor.invoke import failure
from rrotor.result import Result

ROWS, COLUMNS = 50, 1000  # wide, so a long address is never wrapped across lines
KEPT = 256 * 1024  # of what it printed, cleaned: the tail kept
LINE_MAX = 16 * 1024  # of the line in progress
TAIL = 4000  # of that, what a Result carries
SETTLE, SETTLE_MAX = 1.0, 5.0  # once an address shows: quiet this long (or this long at most) before asking
QUIET = 30.0  # no new output this long, still running: ask again
STOP_GRACE = 3.0
TICK = 0.1
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
STEP = "login"
PENDING = {"kind": "login", "key": "cli"}  # never kept in the run's state: the code goes to the command alone

ESCAPES = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC (a hyperlink's target, a title, the clipboard)
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI (colours, cursor moves)
    r"|\x1b[PX^_][^\x1b]*\x1b\\"  # DCS, SOS, PM, APC
    r"|\x1b[@-Z\\-_]|\x1b."  # the rest
)
CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
REPEATS = re.compile(r"(.)\1{8,}")  # a box's border, a spinner's trail
URL = re.compile(r"https?://[^\s<>\"'`]+")
CODE = re.compile(r"(?<![\w-])[A-Z0-9]{4,}-[A-Z0-9]{4,}(?![\w-])")


def _overwritten(line: str) -> str:
    """A line as a terminal shows it: after a carriage return, what follows writes over it from its start."""
    shown = ""
    for part in line.split("\r"):
        shown = part + shown[len(part) :]
    return shown


def clean(raw: str) -> str:
    """What a terminal would show, as text: escape codes gone, a carriage return writing over its line."""
    text = ESCAPES.sub("", raw)
    lines = [_overwritten(line) for line in text.split("\n")]
    return REPEATS.sub(r"\1\1\1", CONTROLS.sub("", "\n".join(lines)))


def urls(text: str) -> list[str]:
    """The addresses in `text`, in order, each once, without trailing punctuation; a loopback one is the CLI's own
    listener, never the address to open."""
    found = [u.rstrip(".,;:)]}>'\"") for u in URL.findall(text)]
    return [u for u in dict.fromkeys(found) if (urlsplit(u).hostname or "") not in LOOPBACK]


def loopback(text: str) -> tuple[str, int, str] | None:
    """A pasted http://localhost… address (the browser's redirect to the CLI's listener): its host, port and path."""
    parts = urlsplit(text.strip())
    host = parts.hostname or ""
    if parts.scheme != "http" or host not in LOOPBACK:
        return None
    try:
        port = parts.port or 80
    except ValueError:
        return None
    return host, port, (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def deliver(host: str, port: int, path: str, connect: Callable[..., Any] = http.client.HTTPConnection) -> int:
    """The redirect requested from the CLI's listener, as the browser would have: answers its HTTP status."""
    hosts = ["127.0.0.1", "::1"] if host == "localhost" else [host]
    error: OSError | None = None
    for each in hosts:
        conn = connect(each, port, timeout=10)
        try:
            conn.request("GET", path, headers={"Host": f"{host}:{port}"})
            status: int = conn.getresponse().status
            return status
        except OSError as err:
            error = err
        finally:
            conn.close()
    raise error or OSError("no address to reach")


def _ctty() -> None:  # pragma: no cover  (runs in the child, between fork and exec)
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class Session:
    """One sign-in command on a pseudo-terminal: started, read all along, answered, stopped."""

    def __init__(
        self,
        spec: dict[str, Any],
        out: IO[bytes] | None = None,
        clock: Callable[[], float] = time.monotonic,
        quiet: float = QUIET,
        settle: tuple[float, float] = (SETTLE, SETTLE_MAX),
    ) -> None:
        self.spec, self.out, self.clock = spec, out or sys.stdout.buffer, clock
        self.quiet, (self.settle, self.settle_max) = quiet, settle
        login = spec.get("login") or {}
        self.code = re.compile(login["code"]) if login.get("code") else CODE
        self.paste = bool(login.get("paste", True))  # it waits for a code pasted back (not a device or polled flow)
        home = (spec.get("env") or {}).get("HOME", "")
        self.signed_in = Path(home, login["signed_in"]) if login.get("signed_in") and home else None
        self.home_files: dict[str, str] = dict(login.get("home_files") or {})
        self.home = Path(home) if home else None
        self.text = ""  # what it printed, cleaned (its tail)
        self.raw = ""  # the line being printed, not yet cleaned
        self.last = clock()  # when it last printed
        self.seen: set[str] = set()  # the addresses already asked about
        self.hidden: list[str] = []  # the answers typed in: never shown back
        self.eof = False
        self.device = False  # the last address asked about came with a device code: an answer is not typed
        self.changed = threading.Condition()
        self.proc: subprocess.Popen[bytes] | None = None
        self.master = -1
        self.before: float | None = None

    # Starting and reading

    def start(self) -> Result | None:
        """The command started on its terminal; a failed Result when it cannot start."""
        if self.home is not None:
            for rel, content in self.home_files.items():
                path = self.home / rel
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content)
        self.before = self._stamp()
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLUMNS, 0, 0))
        env = {str(k): str(v) for k, v in (self.spec.get("env") or {}).items()}
        env.setdefault("TERM", "xterm-256color")
        command = argv(self.spec)
        try:
            self.proc = subprocess.Popen(
                command,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=self.spec.get("cwd"),
                env=env,
                start_new_session=True,
                preexec_fn=_ctty,
            )
        except OSError as error:
            os.close(master)
            return failure(STEP, "E_CLI_EXEC", f"{command[0]} could not start: {error}")
        finally:
            os.close(slave)
        self.master = master
        threading.Thread(target=self._read, daemon=True).start()
        return None

    def _stamp(self) -> float | None:
        try:
            return self.signed_in.stat().st_mtime if self.signed_in else None
        except OSError:
            return None

    def _read(self) -> None:
        while True:
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                chunk = b""
            with self.changed:
                if not chunk:
                    self.eof = True
                    self.changed.notify_all()
                    return
                self.raw += chunk.decode(errors="replace")
                done, _, self.raw = self.raw.rpartition("\n")
                if done:
                    self._took(clean(done + "\n"))
                if len(self.raw) > LINE_MAX:  # a line rewritten in place (a spinner): only its last state shows
                    cut = self.raw.rfind("\r")
                    self.raw = self.raw[cut:] if cut > 0 else self.raw[-LINE_MAX:]
                self.last = self.clock()
                self.changed.notify_all()

    def _took(self, text: str) -> None:
        self.text = (self.text + text)[-KEPT:]
        for line in self._masked(text).splitlines():
            if line.strip():
                self.out.write(line[:2000].encode() + b"\n")
        self.out.flush()

    def _masked(self, text: str) -> str:
        for answer in self.hidden:
            text = text.replace(answer, "[answer]")
        return text

    def shown(self) -> str:
        """What it printed so far, cleaned, the line in progress too."""
        with self.changed:
            return self.text + clean(self.raw)

    # Waiting

    def _signed_in(self) -> bool:
        stamp = self._stamp()
        return stamp is not None and stamp != self.before

    def until_asked(self, mark: int = 0, note: str | None = None) -> Result:
        """Waits for the command to end, to sign in, or to want the person: its Result. `mark`: where in what it
        printed this wait begins (an address before it was asked about already)."""
        assert self.proc is not None  # noqa: S101  (started)
        began, found_at = self.clock(), None
        while True:
            if self.proc.poll() is not None:
                with self.changed:
                    self.changed.wait_for(lambda: self.eof, timeout=0.5)
                return self._done(self.proc.returncode)
            if self._signed_in():
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.proc.wait(STOP_GRACE)
                code = self.proc.poll()
                self.stop()
                return self._done(code)
            shown = self.shown()
            fresh = [u for u in urls(shown[mark:]) if u not in self.seen]
            now = self.clock()
            if fresh:
                found_at = found_at or now
                if now - self.last >= self.settle or now - found_at >= self.settle_max:
                    return self._ask(fresh, shown, note)
            elif now - max(self.last, began) >= self.quiet:
                return self._ask([], shown, note)
            with self.changed:
                self.changed.wait(TICK)

    def _ask(self, fresh: list[str], shown: str, note: str | None) -> Result:
        self.seen.update(fresh)
        url = fresh[0] if fresh else None
        after = shown[shown.rfind("\n", 0, max(shown.find(url), 0)) + 1 :] if url else ""  # from the address's line
        codes = self.code.findall(URL.sub(" ", after)) or self.code.findall(after)
        code = codes[-1] if codes else None
        self.device = not self.paste or code is not None or (self.device and url is None)
        if code:
            prompt = (
                f"Open {url} on your phone, sign in and enter the code {code}. Once it says you are signed in, "
                'continue this run (any answer, e.g. "done").'
            )
        elif url and not self.paste:
            prompt = (
                f"Open {url} on your phone and sign in. Once it says you are signed in, continue this run (any "
                'answer, e.g. "done").'
            )
        elif url:
            prompt = (
                f"Open {url} on your phone and sign in, then continue this run with the code the page shows (or, if "
                "the page ends on an address that does not load, that whole http://localhost… address)."
            )
        else:
            prompt = (
                "The sign-in is still running and waiting (its output is in data.output): continue this run with what "
                "it asks for (typed, then Enter), or with nothing to keep waiting; cancel the run to stop it."
            )
        data = {
            "url": url,
            "urls": fresh,
            "code": code,
            "typed": not self.device,  # whether a continue's answer is typed into the command (a pasted code)
            "output": self._masked(shown)[-TAIL:],
            "schema": {"type": "string"},
            "options": None,
        }
        if note:
            data["note"] = note
            prompt = f"{note} {prompt}"
        return Result("needs_input", data, step=STEP, prompt=prompt)

    def _done(self, code: int | None) -> Result:
        return Result.done(
            {
                "exit_code": code,
                "signed_in": self._signed_in() if self.signed_in else None,
                "output": self._masked(self.shown())[-TAIL:],
            }
        )

    # Answering and stopping

    def answer(self, value: Any) -> Result:
        """The person's answer: typed in (then Enter), or a loopback redirect requested from the CLI's listener.
        After a device code nothing is typed (they signed in on their phone; the command finds out itself). Then
        waits again."""
        text = (value.get("code") or value.get("text")) if isinstance(value, dict) else value
        text = text.strip() if isinstance(text, str) else ""
        mark, note = len(self.shown()), None
        if len(text) >= 4:
            self.hidden.append(text)
        redirect = loopback(text) if text else None
        if redirect is not None:
            try:
                deliver(*redirect)
            except OSError as err:
                note = f"The address could not be opened inside the sandbox ({err})."
        elif text and not self.device:
            os.write(self.master, text.encode() + b"\r")
        return self.until_asked(mark, note)

    def stop(self) -> None:
        """The command and everything it started stopped (its process group), its terminal closed."""
        proc = self.proc
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(STOP_GRACE)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        with self.changed:  # the reader sees its terminal end before the terminal is closed under it
            self.changed.wait_for(lambda: self.eof, timeout=1.0)
        if self.master >= 0:
            with contextlib.suppress(OSError):
                os.close(self.master)
            self.master = -1
