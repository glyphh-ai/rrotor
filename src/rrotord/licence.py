"""The plan's licence: the server's signed {payload, signature}, checked with Glyphh's licence key and kept on /data.
It comes three ways: down the link (`welcome`), from the heartbeat (rrotord/heartbeat.py), or offline, as a token
(base64url of the same JSON) in RROTOR_LICENCE, the file /data/licence.token or, on a local rrotor only,
`PUT /v1/licence`. A licence is live until `expires_at` plus its `grace_days`: past `expires_at` it is in grace, past
grace it has expired. Of the valid licences handed in, the one live longest is kept. A part installs and a run that
needs one goes only while the licence names the part's mod (docs/architecture.md, 10); an expired licence refuses
those, and nothing else."""

from __future__ import annotations

import contextlib
import json
import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from rrotord.errors import BadRequest, Forbidden, RrotorError
from rrotord.keys import canonical, unb64, verify
from rrotord.store import read_json, write_json

log = logging.getLogger("rrotor.licence")
DAY = timedelta(days=1)


def grace_of(payload: dict[str, Any]) -> int | None:
    """`grace_days`: absent is 0; None when it is not a whole number of days."""
    grace = payload.get("grace_days", 0)
    return grace if isinstance(grace, int) and not isinstance(grace, bool) and grace >= 0 else None


def live_until(payload: dict[str, Any] | None) -> datetime | None:
    """`expires_at` plus `grace_days`; None when there is no licence or its dates do not read."""
    if payload is None:
        return None
    grace = grace_of(payload)
    try:
        when = datetime.fromisoformat(str(payload.get("expires_at", "")).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None or grace is None:
        return None
    return when + grace * DAY


def unpack(token: str) -> tuple[Any, Any]:
    """{payload, signature} from an offline token, base64url of its JSON."""
    try:
        signed = json.loads(unb64(token))
    except (RrotorError, ValueError):
        signed = None
    if not isinstance(signed, dict):
        raise BadRequest("The licence token is not base64url of {payload, signature}.", code="E_LICENCE")
    return signed.get("payload"), signed.get("signature")


class Licence:
    def __init__(
        self,
        data: Path,
        key: Ed25519PublicKey | None,
        runtime_id: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.path, self.token_path = data / "licence.json", data / "licence.token"
        self.key, self.runtime_id, self.clock = key, runtime_id, clock
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

    def set(self, payload: Any, signature: Any) -> bool:
        """A licence handed in: refused unless its signature checks with the licence key and it is for this runtime;
        kept unless the one held is live for longer. Answers whether it was kept."""
        if self.key is None:
            raise RrotorError(
                "This rrotor has no licence key (RROTOR_LICENCE_PUBLIC_KEY), so no licence is taken.", code="E_CONFIG"
            )
        if not isinstance(payload, dict) or not isinstance(signature, str):
            raise BadRequest("Send the licence as the server answered it: {payload, signature}.", code="E_LICENCE")
        if not self._checks(payload, signature):
            raise BadRequest("The licence's signature does not check out.", code="E_LICENCE")
        named = payload.get("runtime_id")
        if named is not None and self.runtime_id is not None and str(named) != self.runtime_id:
            raise BadRequest("This licence is for another runtime.", code="E_LICENCE")
        held, given = live_until(self.payload), live_until(payload)
        if held is not None and (given is None or held > given):
            return False
        write_json(self.path, {"payload": payload, "signature": signature})
        self.payload = payload
        return True

    def take(self, sent: Any) -> bool:
        """A licence in any form it is handed in: {payload, signature}, {token} or the token itself."""
        if isinstance(sent, str):
            return self.set(*unpack(sent))
        if isinstance(sent, dict) and isinstance(sent.get("token"), str):
            return self.set(*unpack(sent["token"]))
        if isinstance(sent, dict):
            return self.set(sent.get("payload"), sent.get("signature"))
        raise BadRequest("Send {payload, signature}, {token} or the token itself.", code="E_LICENCE")

    def load(self, token: str | None) -> None:
        """At boot, the offline licences: RROTOR_LICENCE and /data/licence.token. One that is refused is logged."""
        found = [("RROTOR_LICENCE", token)]
        with contextlib.suppress(OSError):
            found.append((str(self.token_path), self.token_path.read_text().strip() or None))
        for source, text in found:
            if text is None:
                continue
            try:
                kept = self.take(text)
            except RrotorError as err:
                log.warning("an offline licence was refused", extra={"source": source, "reason": err.message})
                continue
            log.info("offline licence read", extra={"source": source, "kept": kept, "state": self.state})

    @property
    def state(self) -> str | None:
        """live, grace (past expires_at, within grace_days) or expired; None with no licence."""
        if self.payload is None:
            return None
        until, now = live_until(self.payload), self.clock()
        if until is None or until <= now:
            return "expired"
        return "grace" if until - (grace_of(self.payload) or 0) * DAY <= now else "live"

    @property
    def expired(self) -> bool:
        return self.state in (None, "expired")

    @property
    def licence_id(self) -> str | None:
        found = (self.payload or {}).get("licence_id")
        return str(found) if found is not None else None

    def mods(self) -> list[str]:
        """The mods the licence includes now (in grace too); none when there is no licence or it has expired."""
        if self.payload is None or self.expired:
            return []
        mods = self.payload.get("mods")
        return sorted(mods) if isinstance(mods, dict) else []

    def features(self) -> dict[str, bool]:
        """The plan's features as the licence signed them (`sync`, `ha`, `offline`, `sso`), each a flag; none when
        there is no licence or it has expired. Shown only: nothing here acts on them yet."""
        if self.payload is None or self.expired:
            return {}
        found = self.payload.get("features")
        return {k: v for k, v in sorted(found.items()) if isinstance(v, bool)} if isinstance(found, dict) else {}

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
        """The licence held: its plan, mods and features, and its state (in grace, the days left)."""
        if self.payload is None:
            return None
        until = live_until(self.payload)
        left = 0 if until is None else max(0, math.ceil((until - self.clock()) / DAY))
        return {
            "plan": self.payload.get("plan"),
            "mods": self.mods(),
            "features": self.features(),
            "kind": self.payload.get("kind"),
            "licence_id": self.licence_id,
            "expires_at": self.payload.get("expires_at"),
            "grace_days": grace_of(self.payload),
            "live_until": until.isoformat().replace("+00:00", "Z") if until is not None else None,
            "state": self.state,
            "days_left": left,
            "expired": self.expired,
        }
