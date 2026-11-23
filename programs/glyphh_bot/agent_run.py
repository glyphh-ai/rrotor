"""What one agent run keeps on this machine between its goes (docs/architecture.md, 5.10): a folder of its own in
the program's folder, `agent-runs/<key>/`, the same path each go, so Claude Code finds the session it wrote there.

    config/      Claude Code's own (CLAUDE_CONFIG_DIR): the session's transcript, nothing of the machine's
    work/        the session's working folder (empty: the agent has no file tool of Claude Code's)
    plugins/     the bot's plugins, written from the call each go
    state.json   the session's id, the tokens used so far, the tools used and the files named (names only)

`key` is made from the server's tool address, which names the server's run and is the same in every call of it: a
`continue` and a fresh `call` with `resume` find the same folder. The folder is kept while the run waits and
dropped when it ends; one left behind (a run nobody answered) is swept after STALE seconds."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

ROOT = "agent-runs"
STALE = 7 * 24 * 3600  # a run that waited this long is not coming back
SESSION = re.compile(r"^[A-Za-z0-9-]{8,64}$")
STEPS = 200  # the tool calls a run names
ZERO = {"input_tokens": 0, "output_tokens": 0}


class Kept:
    """One run's folder and what its goes hand on: `session`, `tokens`, `steps`, `files`, `read_bytes`."""

    def __init__(self, files: Path, tool_url: str) -> None:
        self.folder = files / ROOT / hashlib.sha256(tool_url.encode()).hexdigest()[:32]
        self._blank()

    def _blank(self) -> None:
        self.session: str | None = None
        self.tokens: dict[str, int] = dict(ZERO)
        self.steps: list[dict[str, Any]] = []
        self.files: list[dict[str, str]] = []
        self.read_bytes = 0

    @property
    def config(self) -> Path:
        return self.folder / "config"

    @property
    def work(self) -> Path:
        return self.folder / "work"

    @property
    def plugins(self) -> Path:
        return self.folder / "plugins"

    def load(self) -> bool:
        """What the last go left, when its session's transcript is still here: True, and the run carries on."""
        try:
            state = json.loads((self.folder / "state.json").read_text())
            session = str(state["session"])
            if not SESSION.match(session) or not any(self.config.glob(f"projects/*/{session}.jsonl")):
                return False
            self.tokens = {k: int(state["tokens"][k]) for k in ZERO}
            self.steps, self.files = list(state["steps"]), list(state["files"])
            self.read_bytes, self.session = int(state["read_bytes"]), session
        except (OSError, ValueError, KeyError, TypeError):
            self._blank()
            return False
        return True

    def start(self) -> None:
        """A run from its beginning: nothing of an earlier one left."""
        shutil.rmtree(self.folder, ignore_errors=True)
        self._blank()

    def open(self) -> None:
        for folder in (self.config, self.work):
            folder.mkdir(parents=True, exist_ok=True)

    def save(self) -> None:
        state = {
            "session": self.session,
            "tokens": self.tokens,
            "steps": self.steps[-STEPS:],
            "files": self.files,
            "read_bytes": self.read_bytes,
        }
        (self.folder / "state.json").write_text(json.dumps(state))
        os.utime(self.folder)  # sweep reads when the run last went

    def drop(self) -> None:
        """The run ended: its transcript and everything else of it leaves the machine's disk."""
        shutil.rmtree(self.folder, ignore_errors=True)


def sweep(files: Path, now: float | None = None) -> None:
    """Runs left waiting longer than STALE are dropped."""
    root = files / ROOT
    if not root.is_dir():
        return
    cut = (time.time() if now is None else now) - STALE
    for folder in root.iterdir():
        if folder.is_dir() and folder.stat().st_mtime < cut:
            shutil.rmtree(folder, ignore_errors=True)
