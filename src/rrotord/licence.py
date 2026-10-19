"""The licence key held (rrotord/licence_key.py), and the limits it sets. With no key rrotor runs as Community; a
good key lifts that to its own limits until it expires, plus GRACE_DAYS.

Keys come from: GLYPHH_LICENSE_KEY (alias RROTOR_LICENCE), read at start; /etc/rrotor/license.key (alias
/data/licence.token), at start, every hour and on SIGHUP; the heartbeat's answer and the link's `welcome`
(`license_key`); and, on a local rrotor only, `PUT /v1/licence`. Every one found is checked: a bad one is logged at
error with its source (never fatal), and of the good ones the newest (`issued_at`) not past grace is held, ties to
the later `expires_at`; when none is within grace the newest is still held, so the health can say what expired.
Release N also takes the plan's legacy `{payload, signature}` licence wherever one arrives, mapped, and prefers a
key to it. The key held is kept in /data/licence.json as {key, source}, so a restart without the environment keeps
it. Nothing here refuses a run: the worst case is Community's limits.

A key taken, past grace or refused is an audit event (`licence.changed`, `licence.expired`, `licence.bad`), told to
`on_event` with where it came from."""

from __future__ import annotations

import contextlib
import json
import logging
import math
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from rrotord.errors import BadRequest
from rrotord.licence_key import COMMUNITY, GRACE, Key, Plan, Refused, legacy, parse, unpack
from rrotord.store import read_json, write_json

log = logging.getLogger("rrotor.licence")
DAY = timedelta(days=1)
ETC_KEY = Path("/etc/rrotor/license.key")
OLDEST = datetime.min.replace(tzinfo=UTC)


def stamp(when: datetime | None) -> str | None:
    return when.isoformat().replace("+00:00", "Z") if when is not None else None


class Licence:
    def __init__(
        self,
        data: Path,
        runtime_id: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        etc: Path | None = None,
    ) -> None:
        self.path, self.files = data / "licence.json", (etc or ETC_KEY, data / "licence.token")
        self.runtime_id, self.clock = runtime_id, clock
        self.found: dict[tuple[str, bool], Key] = {}  # (source, legacy): the best good key found there
        self.refused: dict[str, str] = {}  # source: why its key was refused, while it is
        self.warned: date | None = None
        self.on_event: Callable[[str, dict[str, Any]], object] | None = None  # audit: changed, expired, bad
        self.expired_told: str | None = None  # the key whose expiry was told
        self._saved()

    def _tell(self, kind: str, **detail: Any) -> None:
        if self.on_event is not None:
            self.on_event(kind, detail)

    def _saved(self) -> None:
        """The key held before the restart, checked again."""
        try:
            saved = read_json(self.path)
        except (OSError, ValueError):
            saved = None
        if not isinstance(saved, dict):
            return
        source = str(saved.get("source") or "saved")
        if isinstance(saved.get("key"), str):
            self.offer(saved["key"], source)
        elif isinstance(saved.get("licence"), dict):
            self.offer(saved["licence"], source)
        elif "payload" in saved:  # kept by a release before the key: the legacy licence itself
            self.offer(saved, source)

    def check(self, sent: Any) -> Key:
        """A key or a legacy licence in any form it arrives in: the key's text, a legacy offline token,
        {token} or {payload, signature}. Raises Refused."""
        if isinstance(sent, str) and "." in sent:
            return parse(sent)
        if isinstance(sent, str):
            return legacy(*unpack(sent), self.runtime_id)
        if isinstance(sent, dict) and isinstance(sent.get("token"), str):
            return self.check(sent["token"])
        if isinstance(sent, dict):
            return legacy(sent.get("payload"), sent.get("signature"), self.runtime_id)
        raise Refused("neither a key nor a legacy licence")

    def offer(self, sent: Any, source: str) -> Key | None:
        """A key found at `source`: kept as that source's if good (and held if it is the best), logged at error if
        not. Answers the key when it was good."""
        try:
            key = self.check(sent)
        except Refused as why:
            self.refused[source] = why.reason
            log.error(f"licence key refused: {why.reason}", extra={"source": source})
            self._tell("licence.bad", source=source, reason=why.reason)
            return None
        self.refused.pop(source, None)
        before = self.held
        kept = self.found.get((source, key.legacy))
        if kept is None or self._rank(key) >= self._rank(kept):  # each source keeps the best it has given
            self.found[(source, key.legacy)] = key
        if self.held is key:
            write_json(self.path, {"key" if not key.legacy else "licence": key.raw, "source": source})
            if before is None or before.raw != key.raw:
                self._tell(
                    "licence.changed",
                    source=source,
                    license_id=key.license_id,
                    tier=key.plan.tier,
                    expires_at=stamp(key.expires_at),
                )
        return key

    def take(self, sent: Any, source: str = "api") -> bool:
        """`PUT /v1/licence` (a local rrotor's): refused 400 E_LICENCE when bad; answers whether it is now held."""
        try:
            key = self.check(sent)
        except Refused as why:
            raise BadRequest(f"The licence key is refused: {why.reason}.", code="E_LICENCE") from None
        return self.offer(key.raw, source) is self.held

    def _rank(self, key: Key) -> tuple[bool, bool, datetime, datetime]:
        return key.state(self.clock()) != "expired", not key.legacy, key.issued_at or OLDEST, key.expires_at

    @property
    def held_from(self) -> tuple[str, Key] | None:
        if not self.found:
            return None
        (source, _), key = max(self.found.items(), key=lambda found: self._rank(found[1]))
        return source, key

    @property
    def held(self) -> Key | None:
        found = self.held_from
        return found[1] if found is not None else None

    def start(self, environment: Iterable[tuple[str, str]]) -> None:
        """At start: the environment's keys, then the files; Community said once when there is none anywhere."""
        for source, text in environment:
            self.offer(text, source)
        self.reread()
        if not self.found and not self.refused:
            log.info("community: no licence key")
        self.warn()

    def reread(self) -> None:
        """The key files, read again: at start, every hour and on SIGHUP (a renewal dropped in, nothing restarted)."""
        for path in self.files:
            text = ""
            with contextlib.suppress(OSError, UnicodeDecodeError):
                text = path.read_text().strip()
            if text:
                sent: Any = text
                with contextlib.suppress(ValueError):
                    sent = json.loads(text)  # a legacy licence as its JSON
                self.offer(sent, str(path))

    def warn(self) -> None:
        """Once a day while the key held has expired: when it did, and when Community's limits apply."""
        held, now = self.held, self.clock()
        if held is not None and held.state(now) == "expired" and self.expired_told != held.raw:
            self.expired_told = held.raw
            self._tell("licence.expired", license_id=held.license_id, expires_at=stamp(held.expires_at))
        if held is None or held.state(now) == "live" or self.warned == now.date():
            return
        self.warned = now.date()
        ends = held.expires_at + GRACE
        when = "from" if held.state(now) == "grace" else "since"
        log.warning(f"licence key expired {held.expires_at.date()}; Community limits {when} {ends.date()}")

    def tick(self) -> None:
        """Hourly, and on SIGHUP."""
        self.reread()
        self.warn()

    @property
    def state(self) -> str:
        """live, grace (past expires_at, within GRACE_DAYS), expired; bad (no good key, one refused); none."""
        held = self.held
        if held is not None:
            return held.state(self.clock())
        return "bad" if self.refused else "none"

    @property
    def community(self) -> bool:
        return self.state not in ("live", "grace")

    def limits(self) -> Plan:
        """What applies now: the key's while it counts, else Community's."""
        held = self.held
        return COMMUNITY if held is None or self.community else held.plan

    @property
    def plan_name(self) -> str:
        """The plan as a caller is told it: Community, or the key's tier (Free, Pro, Team, Enterprise)."""
        return "Community" if self.community else self.limits().tier.capitalize()

    @property
    def licence_id(self) -> str | None:
        held = self.held
        return held.license_id if held is not None else None

    def days_left(self) -> int | None:
        held, now = self.held, self.clock()
        if held is None:
            return None
        until = {"live": held.expires_at, "grace": held.expires_at + GRACE}.get(held.state(now))
        return 0 if until is None else math.ceil((until - now) / DAY)

    def view(self, precedents: int | None = None) -> dict[str, Any]:
        """The key held, its state, the limits that apply (`GET /v1/licence`, the health), and the node's precedents
        against the cap (`over` after a downgrade: nothing deleted, growth refused)."""
        found, limits = self.held_from, self.limits()
        source, held = found if found is not None else (None, None)
        cap = limits.max_precedents
        return {
            "tier": held.plan.tier if held is not None else COMMUNITY.tier,
            "community": self.community,
            "license_id": held.license_id if held is not None else None,
            "customer_id": held.customer_id if held is not None else None,
            "source": source,
            "issued_at": stamp(held.issued_at) if held is not None else None,
            "expires_at": stamp(held.expires_at) if held is not None else None,
            "state": self.state,
            "days_left": self.days_left(),
            "max_nodes": limits.max_nodes,
            "max_precedents": limits.max_precedents,
            "cluster_sync_enabled": limits.cluster_sync_enabled,
            "ha": limits.ha,
            "precedents": {
                "held": precedents,
                "cap": cap,
                "over": precedents is not None and cap is not None and precedents > cap,
            },
        }
