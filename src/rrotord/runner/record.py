"""A run's record on /data/runs/<run_id>/: run.json (what it is and where it stands), log.jsonl, checkpoints in its
state. Inputs stay here, on the machine; nothing of them goes to the Cloud."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from rrotord.store import append_line, read_json, read_lines, write_json

FINAL = frozenset({"done", "failed", "expired", "cancelled"})
WAITING = frozenset({"needs_input", "needs_approval"})
LOG_LINE_MAX = 2000
NODE = re.compile(r"^[a-z0-9-]{1,63}$")


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


@dataclass(slots=True)
class Run:
    id: str
    program: str
    version: int
    commit_sha: str
    function: str
    inputs: dict[str, Any]
    caller: str
    origin: str  # "link" (the Cloud called it) or "local" (the local API)
    status: str = "running"
    subject: str | None = None  # the signed-in person it is for (a CLI runs in their own HOME); None: nobody's
    clearance: float = 1.0  # who asks, as a share of what the program may show (ctx.clearance): 1 sees all
    # the machine's folders it was given, [{path, access}] (rrotord/folders.py): mounted at each of its starts
    folders: list[dict[str, str]] = field(default_factory=list)
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    waiting_since: str | None = None
    ended_at: str | None = None
    seconds: float = 0.0
    pending: dict[str, str] | None = None
    result: dict[str, Any] | None = None
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def final(self) -> bool:
        return self.status in FINAL

    def view(self) -> dict[str, Any]:
        """What the local API shows: never the inputs' values in a list, only on the run itself."""
        return {
            "run_id": self.id,
            "program": self.program,
            "version": self.version,
            "function": self.function,
            "caller": self.caller,
            "origin": self.origin,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "ended_at": self.ended_at,
            "seconds": round(self.seconds, 3),
            "result": self.result,
        }


def new_id(node: str | None = None) -> str:
    """A run's id: a UUID, and in a cluster the node that holds the run before it, `<node id>~<uuid>`
    (docs/specs/cluster.md, 3)."""
    made = str(uuid.uuid4())
    return f"{node}~{made}" if node else made


def holder(run_id: str) -> str | None:
    """The node a run's id names, or None for a plain UUID (a standalone node's run, or one the link opened)."""
    node, sep, _ = run_id.rpartition("~")
    return node if sep else None


def well_formed(run_id: str) -> bool:
    node, sep, rest = run_id.rpartition("~")
    if sep and not NODE.match(node):
        return False
    try:
        uuid.UUID(rest)
    except ValueError:
        return False
    return True


class Records:
    def __init__(self, data: Path) -> None:
        self.root = data / "runs"

    def folder(self, run_id: str) -> Path:
        return self.root / run_id

    def save(self, run: Run) -> None:
        run.updated_at = now()
        write_json(self.folder(run.id) / "run.json", asdict(run))

    def load(self, run_id: str) -> Run | None:
        if not well_formed(run_id):
            return None
        data = read_json(self.folder(run_id) / "run.json")
        if data and "model" in data:  # saved by an earlier release, which named a program `model`
            data["program"] = data.pop("model")
        return Run(**data) if data else None

    def all(self) -> list[Run]:
        if not self.root.is_dir():
            return []
        found = [r for p in self.root.iterdir() if (r := self.load(p.name))]
        return sorted(found, key=lambda r: r.created_at)

    def log(self, run_id: str, entry: dict[str, Any]) -> None:
        line = {"at": now(), **entry}
        for key in ("line", "message"):
            if isinstance(line.get(key), str) and len(line[key]) > LOG_LINE_MAX:
                line[key] = line[key][:LOG_LINE_MAX] + "…"
        append_line(self.folder(run_id) / "log.jsonl", line)

    def lines(self, run_id: str, limit: int = 500) -> list[dict[str, Any]]:
        return read_lines(self.folder(run_id) / "log.jsonl", limit)
