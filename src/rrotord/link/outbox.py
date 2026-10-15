"""What rrotor owes the Cloud: results, events and `deployed`, kept on /data until acknowledged, then dropped.
Results and events take the next number of one rising counter per runtime; a new runtime token starts it again."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from rrotord.store import read_json, write_json


class Outbox:
    def __init__(self, folder: Path) -> None:
        self.path = folder / "outbox.json"
        saved = read_json(self.path, {"seq": 0, "token": None, "pending": []})
        self.seq: int = saved["seq"]
        self.token: str | None = saved["token"]  # the SHA-256 of the token the counter belongs to
        self.pending: list[dict[str, Any]] = saved["pending"]

    def _save(self) -> None:
        write_json(self.path, {"seq": self.seq, "token": self.token, "pending": self.pending})

    def restart(self, token_sha: str) -> None:
        """A new runtime token: the Cloud counts from zero again, so does rrotor; what was owed is dropped."""
        if token_sha != self.token:
            self.seq, self.token, self.pending = 0, token_sha, []
            self._save()

    def add(self, make: Callable[[int], dict[str, Any]], numbered: bool = True) -> dict[str, Any]:
        if numbered:
            self.seq += 1
        message = make(self.seq)
        self.pending.append(message)
        self._save()
        return message

    def ack(self, of: str) -> bool:
        kept = [m for m in self.pending if m["id"] != of]
        if len(kept) == len(self.pending):
            return False
        self.pending = kept
        self._save()
        return True
