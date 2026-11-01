"""A CLI's command as the worker runs it (rrotor/_worker/command.py): its command line, its outputs kept and capped,
what cannot start, and a run stopped mid-command."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from rrotor._worker import command
from rrotor._worker.command import Stream, argv

SH_PATH = {"PATH": "/usr/bin:/bin"}


def script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "tool"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


def run(spec: dict[str, Any]) -> tuple[dict[str, Any], bytes, bytes]:
    out, err = io.BytesIO(), io.BytesIO()
    result = command.run({"env": SH_PATH, **spec}, (out, err))
    return result.to_json(), out.getvalue(), err.getvalue()


def test_the_command_line_per_kind_of_install(monkeypatch):
    assert argv({"run": "exec", "path": "/cli/files/gh", "args": ["pr", "list"]}) == ["/cli/files/gh", "pr", "list"]
    assert argv({"run": "python", "path": "/cli/bin/glyphh"}) == [sys.executable, "/cli/bin/glyphh"]
    monkeypatch.setattr("rrotor._worker.command.shutil.which", lambda name, path: f"{path}/{name}")
    assert argv({"run": "node", "path": "/cli/p/cli.js", "args": ["x"], "env": {"PATH": "/usr/local/bin"}}) == [
        "/usr/local/bin/node",
        "/cli/p/cli.js",
        "x",
    ]
    monkeypatch.setattr("rrotor._worker.command.shutil.which", lambda name, path: None)
    assert argv({"run": "node", "path": "/cli/p/cli.js"})[0] == "node"


def test_a_command_answers_its_exit_code_and_outputs_and_logs_them(tmp_path):
    tool = script(tmp_path, 'cat\necho "cwd: $(pwd)" "env: $X"\necho oops 1>&2\nexit 4\n')
    result, out, err = run({"run": "exec", "path": tool, "stdin": "piped\n", "cwd": str(tmp_path), "env": {"X": "1"}})
    data = result["data"]
    real = os.path.realpath(tmp_path)
    assert (result["status"], data["exit_code"], data["stderr"], data["truncated"]) == ("done", 4, "oops\n", False)
    assert data["stdout"] in (f"piped\ncwd: {tmp_path} env: 1\n", f"piped\ncwd: {real} env: 1\n")
    assert out.splitlines()[0] == b"piped" and err == b"oops\n"
    assert (data["stdout_bytes"], data["stderr_bytes"]) == (len(data["stdout"].encode()), 5)


def test_a_command_that_cannot_start_is_e_cli_exec(tmp_path):
    result, _, _ = run({"run": "exec", "path": str(tmp_path / "missing")})
    assert (result["status"], result["data"]["code"], result["step"]) == ("failed", "E_CLI_EXEC", "cli")


def test_outputs_past_the_cap_are_cut_and_marked(tmp_path):
    tool = script(tmp_path, "printf 'abcdefghij'\n")
    result, out, _ = run({"run": "exec", "path": tool, "cap": 4})
    assert (result["data"]["stdout"], result["data"]["truncated"], out) == ("abcd", True, b"abcdefghij\n")


def test_a_stream_logs_line_by_line_cuts_long_lines_and_stops_past_its_limit(monkeypatch):
    out = io.BytesIO()
    stream = Stream("stdout", 3, out)
    stream.take(b"one\ntw")
    stream.take(b"o\n" + b"x" * (command.LOG_LINE + 5))
    stream.close()
    stream.close()  # nothing left
    lines = out.getvalue().split(b"\n")
    assert lines[:2] == [b"one", b"two"] and len(lines[2]) == command.LOG_LINE and stream.text() == "one"
    monkeypatch.setattr(command, "LOG_MAX", 6)
    capped = io.BytesIO()
    many = Stream("stderr", 10, capped)
    many.take(b"aaaa\nbbbb\ncccc\n")
    assert capped.getvalue() == b"aaaa\n(more stderr not kept)\n"


def test_a_run_stopped_mid_command_stops_the_command(tmp_path, monkeypatch):
    tool = script(tmp_path, "sleep 30\n")
    seen: list[subprocess.Popen[bytes]] = []

    def stopped(proc: subprocess.Popen[bytes], streams: Any) -> None:
        seen.append(proc)
        raise KeyboardInterrupt  # as the worker's Stop does, from a signal

    monkeypatch.setattr(command, "_pump", stopped)
    with pytest.raises(KeyboardInterrupt):
        run({"run": "exec", "path": tool})
    assert seen[0].returncode is not None  # killed and reaped
