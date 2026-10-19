"""The replication log (docs/specs/cluster.md, 3): `/data/cluster/log/` on every node, segment files of JSON lines,
each entry with its `seq` (one rising number per cluster) and the `term` of the head that made it. The head appends
what it captures and decides; a follower appends what it receives, then applies what is committed.

A segment is named by the first seq it holds (`<seq, 20 digits>.jsonl`) and a new one begun past `SEGMENT` bytes.
Kept until every member has applied it (`trim`), and never more than `RROTOR_CLUSTER_LOG_KEEP` bytes: past that the
oldest segments go, and a follower behind them catches up from a snapshot. `base` is the seq the log starts after and
`base_term` its term (kept in `base`, beside the segments, as `<seq> <term>`): 0 for a new log, a snapshot's seq
after a follower installed one, the last seq of the segments trimmed. The newest segment is never trimmed, so the
last seq is always on disk.

Raft (raft.py) asks each entry's term (`term_at`, from runs of one term kept in memory, read again at start) and
drops a diverged tail (`truncate`: the segment holding the seq rewritten, those after removed)."""

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
        self.base, self.base_term = self._base()
        self.last = self.base
        self.runs: list[tuple[int, int]] = []  # (first seq, term) of each run of entries of one term
        for path in self.segments():
            for raw in path.read_bytes().splitlines():
                entry = json.loads(raw)
                self._ran(int(entry["seq"]), int(entry.get("term") or 0))
                self.last = int(entry["seq"])
        segments = self.segments()
        if segments and first_of(segments[0]) - 1 > self.base:  # trimmed before the base was kept with it
            self.base, self.base_term = first_of(segments[0]) - 1, 0
        if segments and self.last < first_of(segments[-1]) - 1:  # an empty newest segment
            self.last = first_of(segments[-1]) - 1

    def _base(self) -> tuple[int, int]:
        try:
            seq, _, term = (self.folder / "base").read_text().partition(" ")
            return int(seq), int(term or 0)
        except (OSError, ValueError):
            return 0, 0

    def _set_base(self, seq: int, term: int) -> None:
        self.base, self.base_term = seq, term
        (self.folder / "base").write_text(f"{seq} {term}")
        self.runs = [(max(first, seq + 1), t) for first, t in self.runs if self._run_end(first) > seq]

    def _ran(self, seq: int, term: int) -> None:
        if not self.runs or self.runs[-1][1] != term:
            self.runs.append((seq, term))

    def _run_end(self, first: int) -> int:
        later = [f for f, _ in self.runs if f > first]
        return (min(later) - 1) if later else self.last

    def segments(self) -> list[Path]:
        return sorted(self.folder.glob("*.jsonl"))

    @property
    def first(self) -> int:
        """The first seq still held; past `last` when the log holds none."""
        return self.base + 1

    def size(self) -> int:
        return sum(p.stat().st_size for p in self.segments())

    def term_at(self, seq: int) -> int | None:
        """The term of the entry at `seq`: `base_term` at the base, None for one not held."""
        if seq == self.base:
            return self.base_term
        if seq < self.first or seq > self.last:
            return None
        return next(term for first, term in reversed(self.runs) if first <= seq)

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
        self._ran(seq, int(entry.get("term") or 0))
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

    def truncate(self, after: int) -> None:
        """The entries past `after` dropped (a tail that diverged from the head's); `after` is at least `base`."""
        for path in reversed(self.segments()):
            if first_of(path) > after:
                path.unlink()
                continue
            kept = [raw for raw in path.read_bytes().splitlines() if json.loads(raw)["seq"] <= after]
            staged = path.with_name(f".{path.name}.tmp")
            staged.write_bytes(b"".join(raw + b"\n" for raw in kept))
            os.replace(staged, path)
            break
        self.last = after
        self.runs = [(first, term) for first, term in self.runs if first <= after]

    def trim(self, applied: int) -> None:
        """Segments every member has applied go (never the newest)."""
        segments = self.segments()
        for path, following in itertools.pairwise(segments):
            if first_of(following) - 1 <= applied:
                self._drop(path, following)

    def _bound(self) -> None:
        """At most `keep` bytes: the oldest segments go first (never the newest)."""
        segments = self.segments()
        total = sum(p.stat().st_size for p in segments)
        for path, following in itertools.pairwise(segments):
            if total <= self.keep:
                break
            total -= path.stat().st_size
            self._drop(path, following)

    def _drop(self, path: Path, following: Path) -> None:
        seq = first_of(following) - 1
        term = self.term_at(seq) or 0
        path.unlink()
        self._set_base(seq, term)

    def reset(self, base: int, term: int = 0) -> None:
        """Everything dropped; the log starts after `base`, of `term` (a follower that installed a snapshot at it)."""
        for path in self.segments():
            path.unlink()
        self.last = base
        self.runs = []
        self._set_base(base, term)


def first_of(path: Path) -> int:
    return int(path.name.split(".")[0])
