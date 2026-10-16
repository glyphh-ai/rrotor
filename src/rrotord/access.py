"""Who may call a hosted rrotor, and whether it is paused: the runtime keys' SHA-256s (never the keys) and the pause
the Cloud set when the plan's hours ran out, both kept in /data/access.json so a restart keeps them. The keys arrive
in the machine's environment at first, then in `welcome` and as `keys` down the link whenever they change (one
revoked is one gone from the list)."""

from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import Path
from typing import Any

from rrotord.store import read_json, write_json

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Access:
    def __init__(self, data: Path, keys: tuple[str, ...] = ()) -> None:
        self.path = data / "access.json"
        saved = read_json(self.path)
        saved = saved if isinstance(saved, dict) else {}
        kept = [k for k in saved.get("keys", []) if isinstance(k, str) and HEX64.match(k)]
        self.keys: list[str] = kept or [k for k in keys if HEX64.match(k)]
        self.paused: dict[str, Any] | None = saved.get("paused") if isinstance(saved.get("paused"), dict) else None
        if not saved and self.keys:
            self._save()

    def _save(self) -> None:
        write_json(self.path, {"keys": self.keys, "paused": self.paused}, private=True)

    def set_keys(self, hashes: list[str]) -> None:
        """The whole list, as the Cloud holds it; a hash that is not one is dropped."""
        self.keys = [h for h in hashes if isinstance(h, str) and HEX64.match(h)]
        self._save()

    def allows(self, token: str) -> bool:
        """Whether this bearer is one of the keys: every hash compared, in constant time, so timing says nothing."""
        presented = digest(token)
        found = False
        for h in self.keys:
            found |= hmac.compare_digest(presented, h)
        return found

    def pause(self, reason: str | None) -> None:
        self.paused = {"reason": reason or "runtime_hours"}
        self._save()

    def resume(self) -> None:
        self.paused = None
        self._save()

    def view(self) -> dict[str, Any]:
        return {"keys": len(self.keys), "paused": self.paused}
