"""The command log of a clone's turns: what was run there, for the person to read while a turn is at work and after
(the work panels' Terminal: rrotord/work.py reads it, `GET /v1/work/{ws}/terminal`). One file per clone in this
program's data folder, `terminal/<owner>__<repo>.jsonl`, a line per entry:

    {"n": 12, "at": "2026-11-22T10:00:00+00:00", "kind": "cmd" | "out" | "err" | "note", "text": "..."}

`cmd` is a command as it was run (git's, by this program; a shell command, by Claude Code), `out` and `err` what a
shell command answered, `note` what else happened (a turn began or ended, a file Claude Code edited). It is a log,
never a shell: nothing is run from it. An entry's text is cut at CLIP characters; when a turn begins the file is cut
to its newest KEEP lines once it has more than MAX. `n` only ever grows, so a reader asks for what is new by number.
A secret never comes here: git's token is in its environment, not its arguments, and Claude Code's key likewise."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

MAX = 2000
KEEP = 1500
CLIP = 4000
SHELL = "Bash"  # Claude Code's tool that runs a command


class Terminal:
    def __init__(self, path: Path | None) -> None:
        """The log at `path`, cut to its newest lines if it grew long; with no path, nothing is kept."""
        self.path, self.n = path, 0
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = path.read_text("utf-8", "replace").splitlines() if path.exists() else []
        if len(lines) > MAX:
            lines = lines[-KEEP:]
            path.write_text("\n".join(lines) + "\n")
        for line in reversed(lines):
            try:
                self.n = int(json.loads(line)["n"]) + 1
            except (ValueError, KeyError, TypeError):
                continue
            break

    def add(self, kind: str, text: str) -> None:
        text = text.strip("\n")
        if self.path is None or not text:
            return
        if len(text) > CLIP:
            text = text[:CLIP] + f"\n… ({len(text) - CLIP} more characters)"
        line = {"n": self.n, "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "kind": kind, "text": text}
        with open(self.path, "a", encoding="utf-8") as log:
            log.write(json.dumps(line, ensure_ascii=False) + "\n")
        self.n += 1


def said(content: Any) -> str:
    """A tool result's text: a string, or its text blocks joined."""
    if isinstance(content, str):
        return content
    blocks = content if isinstance(content, list) else []
    return "\n".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")


def follow(term: Terminal, event: dict[str, Any], shells: set[str]) -> None:
    """One event of Claude Code's stream (`--output-format stream-json`), logged: a shell command it ran and what
    that answered, a file it edited. `shells` remembers which tool calls were commands, to log only their answers."""
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        given = block.get("input") if isinstance(block.get("input"), dict) else {}
        if block.get("type") == "tool_use" and block.get("name") == SHELL:
            shells.add(str(block.get("id")))
            term.add("cmd", str(given.get("command") or ""))
        elif block.get("type") == "tool_use" and isinstance(given.get("file_path"), str):
            term.add("note", f"{block.get('name')} {given['file_path']}")
        elif block.get("type") == "tool_result" and str(block.get("tool_use_id")) in shells:
            term.add("err" if block.get("is_error") else "out", said(block.get("content")))
