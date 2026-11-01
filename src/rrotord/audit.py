"""Audit events: one per thing that happened, who did it and how it ended (docs/specs/cluster.md, 7). Every call by
any route (REST, MCP, the link) and its end, every deploy and its outcome, the runtime keys' changes, the licence
key taken, past grace or refused, every 401 and 403, server-side logging turned on or off.

**Enterprise** (the licence key's tier, while it counts) keeps them in `/data/audit/<YYYY-MM-DD>.jsonl`, one file a
day, each event appended and fsynced; files past `RROTOR_AUDIT_RETENTION_DAYS` go, and, past `RROTOR_AUDIT_MAX_MB`,
the oldest first (never today's), each deletion logged. `GET /v1/audit` and MCP's `audit_query` read them newest
first; a drain reads them forward from its cursor, so none is lost while its endpoint is down. **Below Enterprise**
there is no store: the same events are log lines (`logger: rrotor.audit`), not kept by rrotor.

Who did it is the `actor`: `key` (its SHA-256, never the key), `sign-in` (the subject Glyphh's introspection names),
`local` (the local token), `server` (the link), `rrotor` (its own work: an expired run, a key read from a file), or
`anonymous` (a 401: no credential that counted, the SHA-256 of what was sent if anything was). `client` is the
request's address (the last `X-Forwarded-For` hop, which the packages' proxy sets), kept here and in the customer's
own drain only. Never in an event: a program's inputs or outputs, a token or a key, a request's body; with
`RROTOR_LOG_PAYLOADS=on`, `run.end` keeps its inputs and `Result.data`, 4 KB each, for the customer's drain only."""

from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

from rrotord.errors import BadRequest

log = logging.getLogger("rrotor.audit")
notes = logging.getLogger("rrotor")
DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
PAYLOAD_MAX = 4096
PAYLOADS = ("inputs", "data")  # the fields only the customer's own drain may carry
BLOCK = 64 * 1024
LIMIT, LIMIT_MAX = 100, 1000
ENTERPRISE = "enterprise"


@dataclass(frozen=True, slots=True)
class Who:
    """Who is acting, from where (`client`), by which route (`via`: rest, mcp, link)."""

    actor: dict[str, Any]
    client: str | None = None
    via: str | None = None


RROTOR = Who({"type": "rrotor"})
SERVER = Who({"type": "server"}, None, "link")
WHO: contextvars.ContextVar[Who | None] = contextvars.ContextVar("rrotor_who", default=None)


def current() -> Who:
    """Who the work in hand is for: the request's caller, the link, else rrotor itself."""
    return WHO.get() or RROTOR


@contextlib.contextmanager
def acting(who: Who) -> Iterator[None]:
    token = WHO.set(who)
    try:
        yield
    finally:
        WHO.reset(token)


def client_of(request: web.Request) -> str | None:
    """The caller's address: the last X-Forwarded-For hop (the packages' proxy appends the address it saw), else the
    connection's."""
    hops = [h.strip() for h in request.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
    return hops[-1] if hops else request.remote


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def clip(value: Any) -> str:
    """A payload as JSON, at most 4 KB."""
    text = json.dumps(value, default=str, separators=(",", ":"), ensure_ascii=False)
    raw = text.encode()
    return text if len(raw) <= PAYLOAD_MAX else raw[:PAYLOAD_MAX].decode(errors="ignore")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def stamp(when: dt.datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def moment(text: str, name: str) -> dt.datetime:
    try:
        when = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise BadRequest(f"{name} is an RFC 3339 time (2026-10-19T09:00:00Z).", code="E_BAD_REQUEST") from None
    return when if when.tzinfo is not None else when.replace(tzinfo=dt.UTC)


def backwards(path: Path, end: int) -> Iterator[tuple[int, bytes]]:
    """The lines of a file before byte `end`, the last first, each with its offset."""
    with path.open("rb") as f:
        pos, tail = end, b""
        while pos > 0:
            step = min(BLOCK, pos)
            pos -= step
            f.seek(pos)
            lines = (f.read(step) + tail).split(b"\n")
            start = pos + sum(len(line) + 1 for line in lines) - 1
            for line in reversed(lines[1:]):
                start -= len(line)
                if line.strip():
                    yield start, line
                start -= 1
            tail = lines[0]
        if tail.strip():
            yield 0, tail


def decoded(line: bytes) -> dict[str, Any] | None:
    try:
        event = json.loads(line)
    except ValueError:
        return None  # a line cut short by a crash
    return event if isinstance(event, dict) else None


def matches(event: dict[str, Any], query: dict[str, Any]) -> bool:
    actor = event.get("actor") or {}
    at = moment(str(event.get("at", "")), "at") if event.get("at") else None
    return (
        (query["kind"] is None or event.get("kind") == query["kind"])
        and (query["program"] is None or event.get("program", event.get("model")) == query["program"])  # earlier: model
        and (query["run_id"] is None or event.get("run_id") == query["run_id"])
        and (query["actor"] is None or query["actor"] in (actor.get("type"), actor.get("key_sha256"), actor.get("sub")))
        and (query["since"] is None or (at is not None and at >= query["since"]))
        and (query["until"] is None or (at is not None and at <= query["until"]))
    )


class Audit:
    def __init__(
        self,
        folder: Path,
        node_id: str = "rrotor",
        tier: Callable[[], str] = lambda: "free",
        retention_days: int = 90,
        max_mb: int = 1024,
        payloads: bool = False,
        clock: Callable[[], dt.datetime] = utcnow,
    ) -> None:
        self.folder, self.node_id, self.tier = folder, node_id, tier
        self.retention_days, self.max_bytes, self.payloads, self.clock = retention_days, max_mb << 20, payloads, clock
        self.size: int | None = None  # the folder's bytes, as of the last prune and what was added since
        self.day: str | None = None  # the day last pruned for
        self.cluster_id: str | None = None  # the cluster this node is in, set by rrotord/cluster/node.py

    @property
    def kept(self) -> bool:
        """Whether events are kept here: Enterprise only."""
        return self.tier() == ENTERPRISE

    def emit(
        self, kind: str, *, who: Who | None = None, payload: dict[str, Any] | None = None, **detail: Any
    ) -> dict[str, Any]:
        """One event, kept (Enterprise) or logged; answers it. `payload` (inputs, Result.data) is kept only with
        RROTOR_LOG_PAYLOADS=on and only in the store, never in a log line."""
        actor = who or current()
        event: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "at": stamp(self.clock()),
            "node_id": self.node_id,
            "cluster_id": self.cluster_id,
            "kind": kind,
            "actor": actor.actor,
            "client": actor.client,
            "via": actor.via,
            **detail,
        }
        if not self.kept:
            log.info(kind, extra={k: v for k, v in event.items() if k not in ("at", "node_id")})
            return event
        if self.payloads and payload:
            event.update({k: clip(v) for k, v in payload.items()})
        self._append(event)
        return event

    def days(self) -> list[str]:
        """The day files kept, oldest first."""
        try:
            names = [p.name for p in self.folder.iterdir() if DAY_FILE.match(p.name)]
        except FileNotFoundError:
            return []
        return sorted(names)

    def _append(self, event: dict[str, Any]) -> None:
        today = self.clock().date().isoformat()
        if self.day != today or self.size is None:
            self.prune()
        raw = (json.dumps(event, default=str, separators=(",", ":")) + "\n").encode()
        self.folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.folder / f"{today}.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        self.size = (self.size or 0) + len(raw)
        if self.size > self.max_bytes:
            self.prune()

    def prune(self) -> list[str]:
        """Day files past retention go, then the oldest while the folder is over its size (never today's); each
        deletion logged. Answers the files deleted."""
        now = self.clock()
        today, oldest = now.date().isoformat(), (now - dt.timedelta(days=self.retention_days)).date().isoformat()
        self.day = today
        gone: list[str] = []
        names = self.days()
        sizes = {name: (self.folder / name).stat().st_size for name in names}
        for name in names:
            if name[:10] < oldest:
                gone.append(name)
        for name in names:
            if name in gone or name[:10] == today:
                continue
            if sum(size for n, size in sizes.items() if n not in gone) <= self.max_bytes:
                break
            gone.append(name)
        for name in gone:
            (self.folder / name).unlink(missing_ok=True)
        if gone:
            notes.info(
                "audit files deleted",
                extra={"files": gone, "retention_days": self.retention_days, "max_mb": self.max_bytes >> 20},
            )
        self.size = sum(size for n, size in sizes.items() if n not in gone)
        return gone

    def read(self, cursor: tuple[str, int] | None, limit: int) -> tuple[list[dict[str, Any]], tuple[str, int] | None]:
        """Up to `limit` events after the cursor (a day file and a byte offset), oldest first, and the cursor after
        them: a drain's way through the store. A cursor whose file retention deleted moves to the next kept."""
        marked, after = self.marked(cursor, limit)
        return [event for event, _ in marked], after

    def marked(
        self, cursor: tuple[str, int] | None, limit: int
    ) -> tuple[list[tuple[dict[str, Any], tuple[str, int]]], tuple[str, int] | None]:
        """`read`, each event with the cursor just after it: how a follower sends its events to the cluster's store,
        which takes each once whatever is sent again."""
        files = self.days()
        if not files:
            return [], cursor
        name, offset = cursor or (files[0], 0)
        if name not in files:
            later = [f for f in files if f > name]
            if not later:
                return [], cursor
            name, offset = later[0], 0
        events: list[tuple[dict[str, Any], tuple[str, int]]] = []
        while True:
            with (self.folder / name).open("rb") as f:
                f.seek(offset)
                while len(events) < limit:
                    line = f.readline()
                    if not line.endswith(b"\n"):
                        break  # the end, or a line still being written
                    offset += len(line)
                    event = decoded(line)
                    if event is not None:
                        events.append((event, (name, offset)))
            later = [f for f in files if f > name]
            if len(events) >= limit or not later:
                return events, (name, offset)
            name, offset = later[0], 0

    def query(self, given: dict[str, Any]) -> dict[str, Any]:
        """`GET /v1/audit`: events newest first, filtered by since, until, kind, actor, program and run_id, `limit` at
        a time (100, at most 1000), and the cursor for the next page (None at the end)."""
        query = self._query(given)
        limit, (from_day, from_offset) = query["limit"], query["cursor"]
        events: list[dict[str, Any]] = []
        for name in reversed(self.days()):
            day = name[:10]
            if (from_day is not None and day > from_day) or (
                query["since"] is not None and day < query["since"].date().isoformat()
            ):
                continue
            if query["until"] is not None and day > query["until"].date().isoformat():
                continue
            path = self.folder / name
            end = from_offset if day == from_day else path.stat().st_size
            for offset, line in backwards(path, end):
                event = decoded(line)
                if event is not None and matches(event, query):
                    events.append(event)
                    if len(events) == limit:
                        return {"events": events, "cursor": f"{day}:{offset}"}
        return {"events": events, "cursor": None}

    @staticmethod
    def _query(given: dict[str, Any]) -> dict[str, Any]:
        def text(name: str) -> str | None:
            value = given.get(name)
            if value is None or value == "":
                return None
            if not isinstance(value, str):
                raise BadRequest(f"{name} is a string.")
            return value

        limit = given.get("limit", LIMIT)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 0
        if isinstance(given.get("limit"), bool) or not 1 <= limit <= LIMIT_MAX:
            raise BadRequest(f"limit is 1 to {LIMIT_MAX}.")
        cursor: tuple[str | None, int] = (None, 0)
        if (raw := text("cursor")) is not None:
            day, _, offset = raw.partition(":")
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", day) or not offset.isdigit():
                raise BadRequest("cursor is the one the last page answered.")
            cursor = (day, int(offset))
        since, until = text("since"), text("until")
        return {
            "kind": text("kind"),
            "actor": text("actor"),
            "program": text("program"),
            "run_id": text("run_id"),
            "since": moment(since, "since") if since else None,
            "until": moment(until, "until") if until else None,
            "limit": limit,
            "cursor": cursor,
        }
