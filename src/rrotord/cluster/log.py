"""The replication log (docs/specs/cluster.md, 3): `/data/cluster/log/` on every node, segment files of JSON lines,
each entry with its `seq` (one rising number per cluster) and the head's `term`. The head appends what it captures
and decides; a follower appends what it receives, then applies it in order.

A segment is named by the first seq it holds (`<seq, 20 digits>.jsonl`) and a new one begun past `SEGMENT` bytes.
Kept until every member has applied it (`trim`), and never more than `RROTOR_CLUSTER_LOG_KEEP` bytes: past that the
oldest segments go, and a follower behind them catches up from a snapshot. `base` is the seq the log starts after
(kept in `base`, beside the segments): 0 for a new log, a snapshot's seq after a follower installed one. The newest
segment is never trimmed, so the last seq is always on disk."""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
from typing import Any

SEGMENT = 16 * 1024 * 1024
NAME = "{:020d}.jsonl"


class Log:
    def __init__(self, folder: Path, keep: int) -> None:
        self.folder, self.keep = folder, keep
        folder.mkdir(parents=True, exist_ok=True)
        self.base = self._base()
        self.last = self.base
        segments = self.segments()
        if segments:
            lines = segments[-1].read_bytes().splitlines()
            self.last = json.loads(lines[-1])["seq"] if lines else first_of(segments[-1]) - 1

    def _base(self) -> int:
        try:
            return int((self.folder / "base").read_text())
        except (OSError, ValueError):
            return 0

    def segments(self) -> list[Path]:
        return sorted(self.folder.glob("*.jsonl"))

    @property
    def first(self) -> int:
        """The first seq still held; past `last` when the log holds none."""
        segments = self.segments()
        return first_of(segments[0]) if segments else self.last + 1

    def size(self) -> int:
        return sum(p.stat().st_size for p in self.segments())

    def append(self, entry: dict[str, Any]) -> dict[str, Any]:
        """One entry, written and fsynced: the head's numbered here (no `seq`), a follower's as the head numbered it,
        which must be the next one."""
        seq = entry.setdefault("seq", self.last + 1)
        if seq != self.last + 1:
            raise ValueError(f"entry {seq} does not follow {self.last}")
        segments = self.segments()
        path = segments[-1] if segments and segments[-1].stat().st_size < SEGMENT else self.folder / NAME.format(seq)
        line = json.dumps(entry, separators=(",", ":")).encode() + b"\n"
        with path.open("ab") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        self.last = seq
        self._bound()
        return entry

    def read(self, after: int, limit: int = 100, max_bytes: int = 8 * 1024 * 1024) -> list[dict[str, Any]]:
        """The entries past `after`, in order, at most `limit` of them and about `max_bytes` (always one)."""
        out: list[dict[str, Any]] = []
        size = 0
        segments = self.segments()
        for i, path in enumerate(segments):
            if i + 1 < len(segments) and first_of(segments[i + 1]) <= after + 1:
                continue  # wholly before what is asked
            for raw in path.read_bytes().splitlines():
                entry = json.loads(raw)
                if entry["seq"] <= after:
                    continue
                if out and (len(out) >= limit or size + len(raw) > max_bytes):
                    return out
                out.append(entry)
                size += len(raw)
        return out

    def trim(self, applied: int) -> None:
        """Segments every member has applied go (never the newest)."""
        segments = self.segments()
        for path, following in itertools.pairwise(segments):
            if first_of(following) - 1 <= applied:
                path.unlink()

    def _bound(self) -> None:
        """At most `keep` bytes: the oldest segments go first (never the newest)."""
        segments = self.segments()
        total = sum(p.stat().st_size for p in segments)
        for path in segments[:-1]:
            if total <= self.keep:
                break
            total -= path.stat().st_size
            path.unlink()

    def reset(self, base: int) -> None:
        """Everything dropped; the log starts after `base` (a follower that installed a snapshot at it)."""
        for path in self.segments():
            path.unlink()
        (self.folder / "base").write_text(str(base))
        self.base = self.last = base


def first_of(path: Path) -> int:
    return int(path.name.split(".")[0])
