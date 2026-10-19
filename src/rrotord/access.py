"""Who may call a hosted rrotor, and whether it is paused: the runtime keys' SHA-256s (never the keys) and the pause
the Cloud set when the plan's hours ran out, both kept in /data/access.json so a restart keeps them. The keys arrive
in the machine's environment at first, then in `welcome` and as `keys` down the link whenever they change (one
revoked is one gone from the list). Which key let each request in is counted here too, in memory, and reported up the
link as `usage` once a minute (docs/architecture.md, section 6). A change to the list is an audit event,
`keys.changed`, with the counts and the hashes added and removed (`on_change`)."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rrotord.store import read_json, write_json

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Usage:
    """Calls per runtime key (its SHA-256) since the last report, kept in memory only: a crash loses at most a
    minute's count."""

    def __init__(self, clock: Callable[[], dt.datetime] = utcnow) -> None:
        self.clock = clock
        self.counts: dict[str, tuple[int, dt.datetime]] = {}

    def count(self, key: str) -> None:
        calls = self.counts[key][0] if key in self.counts else 0
        self.counts[key] = (calls + 1, self.clock())

    def merge(self, counted: list[dict[str, Any]]) -> None:
        """A cluster's follower's counts (sent to its head with each ping), added to this node's: the head's `usage`
        is the cluster's."""
        for entry in counted:
            key, calls, at = entry.get("key_sha256"), entry.get("calls"), entry.get("last_used_at")
            if not isinstance(key, str) or not isinstance(calls, int) or not isinstance(at, str):
                continue
            try:
                when = dt.datetime.fromisoformat(at)
            except ValueError:
                continue
            had, last = self.counts.get(key, (0, when))
            self.counts[key] = (had + calls, max(last, when))

    def take(self) -> list[dict[str, Any]]:
        """Every key's calls and when it was last used, and the counts start again."""
        taken = [
            {"key_sha256": key, "calls": calls, "last_used_at": at.isoformat()}
            for key, (calls, at) in self.counts.items()
        ]
        self.counts = {}
        return taken


class Access:
    def __init__(
        self, data: Path, keys: tuple[str, ...] = (), on_change: Callable[[dict[str, Any]], object] | None = None
    ) -> None:
        self.on_change = on_change
        self.path = data / "access.json"
        saved = read_json(self.path)
        saved = saved if isinstance(saved, dict) else {}
        kept = [k for k in saved.get("keys", []) if isinstance(k, str) and HEX64.match(k)]
        self.keys: list[str] = kept or [k for k in keys if HEX64.match(k)]
        self.paused: dict[str, Any] | None = saved.get("paused") if isinstance(saved.get("paused"), dict) else None
        self.usage = Usage()
        if not saved and self.keys:
            self._save()

    def _save(self) -> None:
        write_json(self.path, {"keys": self.keys, "paused": self.paused}, private=True)

    def set_keys(self, hashes: list[str]) -> None:
        """The whole list, as the Cloud holds it; a hash that is not one is dropped."""
        before = set(self.keys)
        self.keys = [h for h in hashes if isinstance(h, str) and HEX64.match(h)]
        self._save()
        added, removed = sorted(set(self.keys) - before), sorted(before - set(self.keys))
        if (added or removed) and self.on_change is not None:
            self.on_change({"keys": len(self.keys), "added": added, "removed": removed})

    def match(self, token: str) -> str | None:
        """Which key this bearer is (its SHA-256), or None: every hash compared, in constant time, so timing says
        nothing."""
        presented = digest(token)
        found: str | None = None
        for h in self.keys:
            if hmac.compare_digest(presented, h):
                found = h
        return found

    def pause(self, reason: str | None) -> None:
        self.paused = {"reason": reason or "runtime_hours"}
        self._save()

    def resume(self) -> None:
        self.paused = None
        self._save()

    def view(self) -> dict[str, Any]:
        return {"keys": len(self.keys), "paused": self.paused}
