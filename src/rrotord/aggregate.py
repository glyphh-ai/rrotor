"""The cluster's aggregate drain and its audit store, at the head (docs/specs/cluster.md, 7; build order step 9).

- **Signals:** a follower's drain (rrotord/drain.py) sends its logs, metrics and traces to its head as `telemetry`
  frames over its own cluster connection (`RROTOR_DRAIN_AT=head`, the default, and always for server-side logging,
  which the head sends for the cluster). The head (`received`) buffers each batch's records for the destinations the
  frame names that it has, and exports them as they came, each with its node's resource (`service.instance.id`,
  `rrotor.node.role`), so the customer's collector sees one endpoint and the customer opens one egress. Across the
  hop the rule is the drain's: buffered, the oldest dropped and counted, on the follower while there is no head and
  on the head while its endpoint is down.
- **Audit events (Enterprise) are never dropped.** Every member keeps its own in `/data/audit/`; the head keeps the
  cluster's, in the same form, under `/data/audit/cluster/` (`ClusterAudit`): its own events copied in, and each
  follower's sent by the follower from a cursor (`/data/audit/.cursor-head`). The head answers each batch with how
  far it holds that node's events (`through`, kept in `/data/audit/cluster/.through.json`) and takes each event once:
  one sent again is skipped, and a follower ahead of what the head holds (a new head, a lost store) goes back to
  where the head is and sends again from there, as long as its retention still holds the files. `GET /v1/audit` and
  `audit_query` read the cluster's store, on a follower by way of the head; the head's drain exports it from its
  own cursor, each event with its node's identity."""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from google.protobuf.message import DecodeError  # type: ignore[import-untyped]

from rrotord.audit import Audit
from rrotord.cluster.transport import ClusterError
from rrotord.config import SIGNALS
from rrotord.errors import BadRequest
from rrotord.store import read_json, write_json

if TYPE_CHECKING:
    from rrotord.cluster.node import Cluster
    from rrotord.drain import Drain

log = logging.getLogger("rrotor.drain")
EVERY = 5.0
BATCH = 512
THROUGH = ".through.json"
CURSOR = ".cursor-head"

Position = tuple[str, int]


def position(value: Any) -> Position | None:
    """A cursor as sent ([day file, offset]) or None; anything else is refused."""
    return None if value is None else exact(value)


def exact(value: Any) -> Position:
    if (
        isinstance(value, list | tuple)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    ):
        return value[0], value[1]
    raise BadRequest("A cursor is [day file, offset].")


def beyond(a: Position | None, b: Position | None) -> bool:
    """Whether a is further into the store than b (None is its start)."""
    return (a or ("", -1)) > (b or ("", -1))


class ClusterAudit(Audit):
    """The cluster's audit store on its head: every member's kept events, in day files as a node keeps its own (by
    the day they arrive, so a drain reading forward never misses one), with the same retention and size."""

    def __init__(self, folder: Path, own: Audit) -> None:
        super().__init__(
            folder,
            own.node_id,
            tier=own.tier,
            retention_days=own.retention_days,
            max_mb=own.max_bytes >> 20,
            clock=own.clock,
        )
        self.own = own
        self.lock = threading.RLock()  # a follower's batch is kept on a thread, the head's own events on the loop

    def through(self) -> dict[str, Position]:
        """How far into each node's own store the events here go."""
        saved = read_json(self.folder / THROUGH)
        out: dict[str, Position] = {}
        for node, at in (saved if isinstance(saved, dict) else {}).items():
            out[node] = (str(at[0]), int(at[1]))
        return out

    def take(self, node: str, since: Position | None, events: list[tuple[dict[str, Any], Position]]) -> Position | None:
        """A node's events read from `since`, each with the position after it: those past what is held kept, in
        order. Answers how far it holds that node's now. A batch from past it (`since` ahead) is not taken: the node
        sends again from the answer. Below Enterprise nothing is kept."""
        with self.lock:
            held = self.through()
            at = held.get(node)
            if not self.kept or beyond(since, at):
                return at
            for event, after in events:
                if beyond(after, at):
                    self._append(event)
                    at = after
            if at is not None and at != held.get(node):
                write_json(self.folder / THROUGH, {**held, node: list(at)})
            return at

    def catch_up(self) -> None:
        """The head's own events, copied in."""
        if not self.kept:
            return
        with self.lock:
            while True:
                at = self.through().get(self.own.node_id)
                events, _ = self.own.marked(at, BATCH)
                if not events:
                    return
                self.take(self.own.node_id, at, events)


class Aggregate:
    """A member's part in the cluster's drain and audit store: on a follower, its kept events sent to the head every
    5 seconds; on the head, its own copied into the cluster's store, and what followers send taken (`received`)."""

    def __init__(
        self,
        audit: Audit,
        store: ClusterAudit,
        cluster: Cluster,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.audit, self.store, self.cluster, self.sleep = audit, store, cluster, sleep
        self.drain: Drain | None = None
        self.cursor = audit.folder / CURSOR

    async def round(self) -> bool:
        """The kept events not yet in the cluster's store put there; answers whether all of them are."""
        role = self.cluster.role
        if not self.audit.kept or role not in ("head", "follower"):
            return True
        if role == "head":
            self.store.catch_up()
            return True
        channel = self.cluster.head_channel
        if channel is None:
            return False
        saved = read_json(self.cursor)
        cursor = (str(saved["file"]), int(saved["offset"])) if isinstance(saved, dict) else None
        while True:
            events, _ = self.audit.marked(cursor, BATCH)
            try:
                got = await channel.request(
                    "telemetry",
                    signal="audit",
                    since=list(cursor) if cursor else None,
                    events=[[after[0], after[1], event] for event, after in events],
                )
                through = position(got.get("through"))
            except (ClusterError, TimeoutError, ConnectionError, aiohttp.ClientError) as err:
                log.debug("the head did not take audit events", extra={"reason": str(err)})
                return False
            if through == cursor:
                return not events  # all of them there; or the head not taking them now
            cursor = through
            write_json(self.cursor, {"file": cursor[0], "offset": cursor[1]} if cursor else None)

    async def run(self) -> None:
        while True:
            await self.sleep(EVERY)
            try:
                await self.round()
            except Exception:
                log.exception("sending audit events to the head failed")

    async def received(self, node: str, msg: dict[str, Any]) -> dict[str, Any]:
        """A member's `telemetry`, at the head: kept audit events into the cluster's store (answered with how far it
        holds that node's), or a drain batch for the head's own destinations (answered with how many records)."""
        if self.cluster.role != "head":
            raise ClusterError("This node is not the head.", code="E_NOT_HEAD")
        signal = msg.get("signal")
        if signal == "audit" and "events" in msg:
            sent = msg.get("events")
            if not isinstance(sent, list) or not all(
                isinstance(e, list) and len(e) == 3 and isinstance(e[2], dict) for e in sent
            ):
                raise BadRequest("Audit events are [[day file, offset, event], ...].")
            events = [(e[2], exact(e[:2])) for e in sent]
            through = await asyncio.to_thread(self.store.take, node, position(msg.get("since")), events)
            return {"through": list(through) if through else None}
        to, body = msg.get("to"), msg.get("body")
        if signal not in SIGNALS or not isinstance(to, list) or not isinstance(body, str):
            raise BadRequest("Telemetry is {signal, to, body}.")
        if self.drain is None:
            return {"taken": 0}
        try:
            taken = self.drain.received(str(signal), base64.b64decode(body, validate=True), [str(t) for t in to])
        except (ValueError, DecodeError):
            raise BadRequest("The telemetry's body is not an OTLP export request.") from None
        return {"taken": taken}
