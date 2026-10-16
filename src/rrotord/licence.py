"""The plan's licence, as the mod hands it in (PUT /v1/licence): the server's signed {payload, signature}, checked
with Glyphh's licence key and kept on /data. A part installs and a run that needs one goes only while the licence
names the part's mod (docs/architecture.md, 10)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from rrotord.errors import BadRequest, Forbidden, RrotorError
from rrotord.keys import canonical, verify
from rrotord.store import read_json, write_json


class Licence:
    def __init__(self, data: Path, key: Ed25519PublicKey | None) -> None:
        self.path = data / "licence.json"
        self.key = key
        self.payload: dict[str, Any] | None = None
        saved = read_json(self.path)
        if isinstance(saved, dict) and self._checks(saved.get("payload"), saved.get("signature")):
            self.payload = saved["payload"]

    def _checks(self, payload: Any, signature: Any) -> bool:
        if self.key is None or not isinstance(payload, dict) or not isinstance(signature, str):
            return False
        try:
            return verify(self.key, signature, canonical(payload))
        except RrotorError:
            return False

    def set(self, payload: Any, signature: Any) -> None:
        """A licence from the mod: kept only when its signature checks with the licence key."""
        if self.key is None:
            raise RrotorError(
                "This rrotor has no licence key (RROTOR_LICENCE_PUBLIC_KEY), so no licence is taken.", code="E_CONFIG"
            )
        if not isinstance(payload, dict) or not isinstance(signature, str):
            raise BadRequest("Send the licence as the server answered it: {payload, signature}.", code="E_LICENCE")
        if not self._checks(payload, signature):
            raise BadRequest("The licence's signature does not check out.", code="E_LICENCE")
        write_json(self.path, {"payload": payload, "signature": signature})
        self.payload = payload

    @property
    def expired(self) -> bool:
        when = str((self.payload or {}).get("expires_at", ""))
        try:
            return datetime.fromisoformat(when.replace("Z", "+00:00")) <= datetime.now(UTC)
        except ValueError:
            return True

    def mods(self) -> list[str]:
        """The mods the licence includes now; none when there is no licence or it has expired."""
        if self.payload is None or self.expired:
            return []
        mods = self.payload.get("mods")
        return sorted(mods) if isinstance(mods, dict) else []

    def require(self, mod: str) -> None:
        """Refused with E_NOT_IN_PLAN unless the licence includes `mod` now."""
        if mod not in self.mods():
            why = (
                "no licence"
                if self.payload is None
                else ("the licence has expired" if self.expired else "not in the plan")
            )
            raise Forbidden(f"{mod} is not in this machine's plan: {why}.", code="E_NOT_IN_PLAN", mod=mod)

    def view(self) -> dict[str, Any] | None:
        if self.payload is None:
            return None
        return {
            "plan": self.payload.get("plan"),
            "mods": self.mods(),
            "expires_at": self.payload.get("expires_at"),
            "expired": self.expired,
        }
