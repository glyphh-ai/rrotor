"""This node in its cluster (docs/specs/cluster.md, 3, 4 and 5): membership, and replication with a fixed head
(replication.py).

**Gated by the licence key.** With `RROTOR_CLUSTER=on` the transport starts only while the key held has
`cluster_sync_enabled`; under Community (or a key without it) the listener never starts, rrotor logs `cluster
synchronisation disabled under the Community licence` (or `under this licence`) at warning and runs standalone. When
a key stops allowing it, a head runs on standalone and tells its followers, which become refused
(`E_LICENCE_CLUSTER`, their data kept); a new key resumes the cluster from there. The gate is looked at again every
10 seconds and whenever the licence changes.

**Finding the head.** A node dials every address discovery gives (discovery.py), itself among them, and says hello;
each answers who it is, its cluster and its head. A node that hears of a head asks it to join. With no cluster
anywhere, the node named by `RROTOR_CLUSTER_HEAD`, else the lowest node id once every address has answered, makes
the cluster: a new `cluster_id`, term 1, itself head (`cluster.head`). Every other node waits for it (`no_cluster`).
A node that has ever heard of a cluster never makes one.

**Joining, and `max_nodes`.** The head admits a node unless the cluster already has the key's `max_nodes` members
(`E_CLUSTER_FULL`), a live member has its id (`E_CLUSTER_NODE_ID`), it belongs to another cluster (`E_CLUSTER_ID`),
or it left and was not asked back by `rrotord cluster join` (`E_CLUSTER_LEFT`). Each refusal is logged and audited
(`cluster.refused`) on the head and logged at error on the node, which keeps its `/data`, answers 503 to everything
but `/healthz` and asks again every minute. A member that rejoins (a restart, a lost connection) is let back in
without counting again. A key with a smaller `max_nodes` makes the head remove the members joined last until the
cluster fits; a member unseen for `RROTOR_CLUSTER_EVICT_AFTER` is removed (`cluster.leave`).

**Leaving** is `rrotord cluster leave <node>` (command.py), run on any node and authenticated by the cluster secret:
a follower passes it to the head. The node removed is told, and does not ask again until `rrotord cluster join` is
run on it. The head cannot leave: another node is promoted first.

**Membership is in the log** (raft.py): the head commits a `member` entry for each node it admits (or whose address
or version changed), removes or evicts, carrying the members after it in order of joining, each a voter or a learner.
Changes go one at a time: a change waits until the last one is held by a majority (`E_CLUSTER_BUSY` meanwhile, and
the node asks again).

**Terms and fencing.** Every append, snapshot, vote, join and forward carries the term; a node refuses anything of a
lower term and takes a higher one at once. A head that sees a higher term steps down there and then: its shippers
stop, its followers' connections close, its in-flight writing runs fail (`E_NO_HEAD`), and its stores may hold
writes no log has, so it takes the new head's snapshot before it follows (`stale`).

**Manual failover (Pro, Team):** `rrotord cluster promote <node> [--force] [--yes]` (command.py), on any node: the
node named asks every member who it is (its term, its log); with a majority of members reachable, a log behind
none of theirs (else `E_CLUSTER_LAG` with each member's lag, unless `--force`) and the head either reachable or
confirmed down (`E_CLUSTER_CONFIRM`, unless `--yes`), it asks every member for its vote at the next term, and a
majority makes it head (`cluster.head`, `promoted`). A forced vote is given only by a member with the same members
as the candidate, so two heads can never share a term. A head restarted without HA leads again in its term unless a
member it reaches knows a higher one; a head that has lost its followers looks for one every few seconds, so an old
head that comes back is fenced by the promoted one's term.

**Replication.** The head ships its log to each follower it admits, from what the follower says it applied (a
snapshot first for a new node or one behind the log), over the follower's own connection: `append` and `snapshot`
are the head's requests, answered as Raft does. A joined follower is `catching_up` (not ready) until it has applied
what the head had committed, and again once behind it for more than a second. `GET /cluster/v1/versions/
<model>/<commit>.tar` and `GET /cluster/v1/snapshot` are plain requests on the listener, for members only.

**Forwarding** (`forward`): a follower sends a writing call, a deploy, and anything about a run another node holds
to its head, with the caller as data (who, from where, by which route), a deadline and its term; the head does it
(as that caller), or passes it to the follower that holds the run (`E_RUN_LOST`, 410, when that node is not
connected), and answers with the seq its log stands at. A follower with no head, or whose head has been deposed,
answers `E_NO_HEAD` (503). What a forward does is the API's own operations (`rrotord/api/forward.py`,
`serve_forward`). Each ping of a follower carries its applied seq and its runtime keys' calls since the last, which
the head adds to its own `usage`.

**Elected heads (HA, Enterprise: `ha` in the licence).** A ticker looks at Raft's clock: a voter whose head has been
silent for its election timeout stands (`vote` asked of every other voter on a connection of its own) and, elected,
leads (`cluster.head`, `elected`); a head out of touch with a majority of voters steps down (`no_majority`). A write
done on the head is answered once a majority of voters hold it (`committed`: `E_NOT_COMMITTED` after 5 s, the entry
kept), and `E_NO_HEAD` carries `Retry-After: 2`. An elected head restarted follows whoever is elected, its stores
taken again first. Under HA `promote` moves the head only to a voter, never forced (`E_CLUSTER_HA`).

**Telemetry** (`telemetry`): a follower's drain batches and kept audit events, to its head, over its own connection;
what the head does with them is rrotord/aggregate.py's (`on_telemetry`)."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from rrotord import log as logs
from rrotord.access import Access
from rrotord.audit import WHO, Audit, Who
from rrotord.cluster.discovery import Discovery, Resolve, getaddrinfo
from rrotord.cluster.raft import majority
from rrotord.cluster.replication import Replication
from rrotord.cluster.tls import COMMAND, Credentials, peer_name
from rrotord.cluster.transport import MESSAGE_MAX, PATH, Channel, ClusterError, answer, dial, failed, fetch, frame
from rrotord.config import NODE_ID, Config
from rrotord.errors import BadRequest, RrotorError, Unavailable
from rrotord.licence import Licence
from rrotord.models import Models
from rrotord.store import read_json, write_json

log = logging.getLogger("rrotor.cluster")
BIND = "0.0.0.0"  # noqa: S104  (the cluster's port is opened between the nodes only, by the packages)
RETRY = 2.0  # between tries to find the head or make the cluster
REJOIN = 60.0  # a refused node asks again
PING = 5.0  # a follower pings its head, which counts it seen
GATE = 10.0  # the licence's gate looked at again
HEAD_EVERY = 5.0  # the head checks the cap and the members unseen
SHUTDOWN = 5.0  # seconds the listener waits for its connections' handlers when it stops
ASKED_WITHIN = 10.0  # `rrotord cluster join` waits this long for the outcome
REFUSED_KEPT = 50
VOTE_WITHIN = 5.0  # a vote's answer, asked by `rrotord cluster promote`
TICK = 0.05  # with HA, Raft's clock looked at this often: an election timeout passed, a head out of touch
COMMIT_WITHIN = 5.0  # with HA, a write is answered once a majority of voters hold it, or E_NOT_COMMITTED
RETRY_AFTER = "2"  # E_NO_HEAD's Retry-After: an election takes 1.5 to 3 s
CHANGE_WITHIN = 5.0  # `rrotord cluster leave` waits this long for the last membership change to be committed
FORWARD_WITHIN = 3630.0  # a forwarded request's answer: a run's longest seconds (3600) and its end
READ_YOUR_WRITES = 2.0  # a node waits this long to have applied a seq it was asked to be at
REFUSALS = frozenset({"E_CLUSTER_FULL", "E_CLUSTER_NODE_ID", "E_CLUSTER_LEFT", "E_CLUSTER_ID"})
NODE = Who({"type": "rrotor"}, None, "cluster")
OPERATOR = Who({"type": "local"}, None, "cluster")  # `rrotord cluster ...`, on a node, with the cluster secret


async def _nothing() -> None:
    return None


def no_head(message: str = "No head answers this node: writes wait for it.") -> Unavailable:
    return Unavailable(message, code="E_NO_HEAD", headers={"Retry-After": RETRY_AFTER})


async def _no_telemetry(node: str, msg: dict[str, Any]) -> dict[str, Any]:
    raise ClusterError("This node takes no telemetry.", code="E_CLUSTER_MESSAGE")


def names(members: Any) -> list[str]:
    return [str(m.get("node_id")) for m in members or [] if isinstance(m, dict)]


def stamp(when: float) -> str:
    return dt.datetime.fromtimestamp(when, dt.UTC).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class Member:
    node_id: str
    address: str
    version: str
    instance: str  # the node's own random id, kept on its /data: tells a restart from a second node with its id
    joined_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "address": self.address,
            "version": self.version,
            "instance": self.instance,
            "joined_at": self.joined_at,
        }

    @classmethod
    def of(cls, data: Any) -> Member | None:
        if not isinstance(data, dict) or not all(
            isinstance(data.get(k), str) for k in ("node_id", "address", "version", "instance", "joined_at")
        ):
            return None
        return cls(data["node_id"], data["address"], data["version"], data["instance"], data["joined_at"])


class Cluster:
    def __init__(
        self,
        config: Config,
        licence: Licence,
        audit: Audit,
        version: str,
        resolve: Resolve = getaddrinfo,
        wall: Callable[[], float] = time.time,
        models: Models | None = None,
        access: Access | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config, self.licence, self.audit, self.version, self.wall = config, licence, audit, version, wall
        self.clock = clock
        self.node_id, self.address, self.port = config.node_id, config.advertise, config.cluster_port
        self.discovery = Discovery(config.cluster_peers, config.cluster_dns, config.cluster_port, resolve)
        self.path = config.data / "cluster" / "state.json"
        saved = read_json(self.path) if config.cluster else None
        self.state: dict[str, Any] = saved if isinstance(saved, dict) else {}
        self.role = "standalone"  # head or follower while a member; a node that is not one is standalone
        self.status = "joining" if config.cluster else "standalone"  # ready, joining, no_cluster, refused, standalone
        self.refusal: dict[str, Any] | None = None  # this node's, while it is refused
        self.refused: list[dict[str, Any]] = []  # the head's: the nodes it refused, the newest last
        self.links: dict[str, Channel] = {}  # the head's: each follower's connection
        self.accepted: set[Channel] = set()  # every connection the listener holds, closed when it stops
        self.seen: dict[str, float] = {}  # the head's: when each member was last heard from
        self.told: dict[str, Any] = {}  # a follower's: what the head last said (members, refused)
        self.head_channel: Channel | None = None  # a follower's connection to its head
        self.credentials: Credentials | None = None
        self.session: aiohttp.ClientSession | None = None
        self.listener: web.AppRunner | None = None
        self.supervisor: asyncio.Task[None] | None = None
        self.membership: asyncio.Task[None] | None = None
        self.gated: bool | None = None  # whether the key allowed sync when last looked at
        self.changed = asyncio.Event()  # the licence changed: look at the gate now
        self.wake = asyncio.Event()  # `rrotord cluster join`: ask now
        self.attempted = asyncio.Event()
        self.asked = False
        self.telling: set[asyncio.Task[None]] = set()  # the head telling removed nodes so; a follower answering
        self.access = access or Access(config.data)
        self.ha: Callable[[], bool] = lambda: licence.limits().ha  # Enterprise: the cluster elects its head
        self.replication = Replication(
            config.data / "cluster" / "log",
            config.run_dir / "cluster" / "scratch",
            config.cluster_log_keep,
            models or Models(config.data),
            self.access,
            licence,
            role=lambda: self.role,
            saved=self._applied,
            applied=int(self.state.get("applied_seq") or 0),
            wall=wall,
            me=self.node_id,
            state=self.state,
            persist=self._persist,
            ha=lambda: self.ha(),
            clock=clock,
        )
        self.raft = self.replication.raft
        if self.state.get("head") == self.node_id:  # a head's stores hold all its log: it applied as it wrote
            self.replication.applied = max(self.replication.applied, self.replication.log.last)
        self.replication.fetch = self._fetch
        self.replication.moved = self._reconcile
        self.replication.member = self._member_applied
        self.following: tuple[str, int] | None = None  # a follower's: the head it follows and its term
        self.leading = asyncio.Lock()
        self.said: dict[str, dict[str, str]] = {}  # the head's: each member's address, version, instance at join
        self.promoting = False  # `rrotord cluster promote` in hand: it leads, as the operator
        self.led = 0  # the term this node last led in
        self.ticking: asyncio.Task[None] | None = None
        self.campaigning: asyncio.Task[None] | None = None
        self.deposed: Callable[[], Awaitable[None]] = _nothing  # a head that steps down: its writing runs fail
        # what a forwarded request does: the API's operations, set by rrotord/api/app.py
        self.serve_forward: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
        self.usage: Callable[[list[dict[str, Any]]], None] = lambda _: None  # the head: a follower's key calls
        self.taken: Callable[[], list[dict[str, Any]]] = list  # a follower: its key calls since the last ping
        self.roled: Callable[[], None] = lambda: None  # this node's role changed (the link looks again)
        # a member's telemetry at the head: rrotord/aggregate.py, set by the daemon
        self.on_telemetry: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] = _no_telemetry

    # What the rest of rrotor reads

    @property
    def cluster_id(self) -> str | None:
        """The cluster this node is in or asking to join; None while it is standalone."""
        if self.status == "standalone":
            return None
        cluster_id = self.state.get("cluster_id") or self.state.get("heard")
        return str(cluster_id) if cluster_id else None

    def identity(self) -> tuple[str | None, str]:
        """(cluster_id, role) for the heartbeat."""
        return self.cluster_id, self.role

    @property
    def shown(self) -> str:
        """The status as told: a follower that is a member but not in step with its head is `catching_up`."""
        if self.status == "ready" and self.role == "follower" and not self.replication.current():
            return "catching_up"
        return self.status

    def ready(self) -> tuple[bool, str | None, str | None]:
        """Whether this node may take traffic (`/readyz`), else why: joining, catching_up, refused, no_cluster."""
        status = self.shown
        if status in ("standalone", "ready"):
            return True, None, None
        code = self.refusal.get("code") if self.refusal is not None else None
        if status == "catching_up" and self.replication.failure is not None:
            code = self.replication.failure["code"]
        return False, status, code

    @property
    def member(self) -> bool:
        """A head or a follower: what syncs is the head's to write."""
        return self.role in ("head", "follower")

    def _applied(self, seq: int) -> None:
        self.state["applied_seq"] = seq
        self._save()

    def _persist(self, values: dict[str, Any]) -> None:
        """Raft's term, vote, head and members, kept before it answers anyone."""
        if values.get("head") != self.state.get("head"):
            found = next((m for m in self.members_of(values["members"]) if m.node_id == values.get("head")), None)
            self.state["head_address"] = found.address if found is not None else None
        self.state.update(values)
        self._save()

    @staticmethod
    def members_of(members: Any) -> list[Member]:
        found = (Member.of(m) for m in members or [])
        return [m for m in found if m is not None]

    def members(self) -> list[Member]:
        return self.members_of(self.raft.members)

    def _members_view(self) -> list[dict[str, Any]]:
        if self.role != "head":
            return list(self.told.get("members") or [])
        now, voters = self.wall(), set(self.raft.voters())
        return [
            {
                "node_id": m.node_id,
                "address": m.address,
                "role": "head" if m.node_id == self.node_id else "follower",
                "voter": m.node_id in voters,
                "applied_seq": self._applied_by(m.node_id),
                "lag": max(self.replication.log.last - self._applied_by(m.node_id), 0),
                "last_seen": stamp(now if m.node_id == self.node_id else self.seen.get(m.node_id, now)),
                "connected": m.node_id == self.node_id or m.node_id in self.links,
                "version": m.version,
                "instance": m.instance,
                "joined_at": m.joined_at,
            }
            for m in self.members()
        ]

    def _applied_by(self, node: str) -> int:
        return self.replication.log.last if node == self.node_id else self.replication.acked.get(node) or 0

    def _reason(self) -> str:
        return f"cluster synchronisation disabled under {'the Community' if self.licence.community else 'this'} licence"

    def view(self) -> dict[str, Any]:
        """`GET /v1/cluster` and `cluster_status`."""
        if not self.config.cluster:
            return {"role": "standalone"}
        if self.status == "standalone":
            return {"role": "standalone", "node_id": self.node_id, "status": "standalone", "reason": self._reason()}
        limits = self.licence.limits()
        return {
            "cluster_id": self.cluster_id,
            "node_id": self.node_id,
            "role": self.role,
            "status": self.shown,
            "head": self.state.get("head"),
            "term": self.raft.term,
            "commit_seq": self.replication.commit_seq if self.member else 0,
            "applied_seq": self.replication.applied_seq if self.member else 0,
            "tier": limits.tier,
            "max_nodes": limits.max_nodes,
            "ha": self.ha(),
            "members": self._members_view(),
            "refused": list(self.refused) if self.role == "head" else list(self.told.get("refused") or []),
            "refusal": self.refusal,
        }

    def short(self) -> dict[str, Any]:
        """The health's `cluster`."""
        if self.status == "standalone":
            return {"role": "standalone"}
        ready, why, _ = self.ready()
        return {
            "cluster_id": self.cluster_id,
            "node_id": self.node_id,
            "role": self.role,
            "status": self.shown,
            "ready": ready,
            "head": self.state.get("head"),
            "term": self.raft.term,
            "members": len(self.members()),
            "reason": why,
        }

    def licence_changed(self) -> None:
        self.changed.set()
        self.replication.licence_changed()  # the head ships a new key held

    # Running

    async def start(self) -> None:
        """With the cluster on: watch the licence's gate, which starts and stops the transport."""
        if self.config.cluster and self.supervisor is None:
            self.supervisor = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        if self.supervisor is not None:
            self.supervisor.cancel()
            await asyncio.gather(self.supervisor, return_exceptions=True)
            self.supervisor = None
        await self._shut()
        for task in list(self.telling):
            task.cancel()
        await asyncio.gather(*self.telling, return_exceptions=True)
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def _supervise(self) -> None:
        while True:
            enabled = self.licence.limits().cluster_sync_enabled
            if enabled != self.gated:
                self.gated = enabled
                try:
                    await (self._open() if enabled else self._close())
                except Exception:
                    log.exception("cluster: the transport could not start")
                    self.gated = None
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), GATE)
            self.changed.clear()

    def _save(self) -> None:
        write_json(self.path, self.state)

    def _identify(self) -> None:
        """Every log line and audit event names the cluster and this node's role while it is in one."""
        cluster_id = self.cluster_id
        self.audit.cluster_id = cluster_id
        self.roled()
        if cluster_id is None:
            logs.identify(node_id=self.node_id)
        else:
            logs.identify(node_id=self.node_id, cluster_id=cluster_id, role=self.role)

    async def _open(self) -> None:
        if "instance" not in self.state:
            self.state["instance"] = str(uuid.uuid4())
            self._save()
        if self.credentials is None:
            folder = self.config.run_dir / "cluster"
            self.credentials = Credentials(list(self.config.cluster_secrets), self.node_id, folder)
        if self.session is None:
            self.session = aiohttp.ClientSession()
        if self.refusal is not None and self.refusal["code"] == "E_LICENCE_CLUSTER":
            self.refusal = None
        self.status = "joining" if self.refusal is None else "refused"
        app = web.Application()
        app.router.add_get(PATH, self._accept)
        app.router.add_get("/cluster/v1/versions/{model}/{commit}.tar", self._version)
        app.router.add_get("/cluster/v1/snapshot", self._snapshot)
        self.listener = web.AppRunner(app, access_log=None, shutdown_timeout=SHUTDOWN)
        await self.listener.setup()
        await web.TCPSite(self.listener, BIND, self.port, ssl_context=self.credentials.server).start()
        log.info("cluster: listening", extra={"port": self.port, "address": self.address})
        self.membership = asyncio.create_task(self._membership())
        self.ticking = asyncio.create_task(self._elections())

    async def _close(self) -> None:
        log.warning(self._reason())
        if self.role == "head":
            for channel in list(self.links.values()):
                await self._tell_removed(
                    channel, "E_LICENCE_CLUSTER", "The licence key no longer allows cluster synchronisation."
                )
            self.status = "standalone"
        elif self.state.get("cluster_id") and self.state.get("head") != self.node_id:
            self._refuse("E_LICENCE_CLUSTER", "The licence key does not allow cluster synchronisation.")
        else:
            self.status = "standalone"
        self.role = "standalone"
        await self._shut()
        self._identify()

    async def _shut(self) -> None:
        await self.replication.stop()
        for task in (self.membership, self.ticking, self.campaigning):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.membership = self.ticking = self.campaigning = None
        for channel in [*self.accepted, *([self.head_channel] if self.head_channel else [])]:
            await channel.close()
        self.links.clear()
        self.head_channel = None
        if self.listener is not None:
            await self.listener.cleanup()
            self.listener = None

    async def _membership(self) -> None:
        while True:
            try:
                wait = await self._member()
            except Exception:
                log.exception("cluster: finding the head failed")
                wait = RETRY
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), wait)
            self.wake.clear()

    async def _member(self) -> float:
        """One step: lead, or find the head and join it (following it until the connection drops), or make the
        cluster. Answers how long to wait before the next."""
        if self.state.get("cluster_id"):
            if self.role == "head":
                await self._fence()
                if self.role == "head":
                    self._fit()
                    self._evict()
                    self._refresh()
                    return HEAD_EVERY
            elif self.raft.role == "head" and not self.promoting:  # elected, or a head the gate closed and opens
                await self._lead("elected" if self.raft.via == "votes" else "resumed")
                return HEAD_EVERY
            elif self.state.get("head") == self.node_id and self.refusal is None:
                if not self.ha():
                    if await self._resume():
                        return HEAD_EVERY
                else:  # an elected head restarted follows whoever is elected, its stores taken again first
                    self.raft.forget()
        if self.refusal is not None and self.refusal["code"] == "E_CLUSTER_LEFT" and not self.asked:
            return REJOIN  # it left: it asks again only when `rrotord cluster join` says so
        answers, everyone = await self._survey()
        self._observe(answers)
        heads = sorted(answers, key=lambda a: (a.get("role") != "head", -int(a.get("term") or 0)))
        found = next((a for a in heads if a.get("cluster_id") and a.get("head_address")), None)
        if found is not None:
            if self.state.get("heard") != found["cluster_id"]:
                self.state["heard"] = found["cluster_id"]
                self._save()
            return await self._join(str(found["head_address"]))
        if self.state.get("cluster_id") or self.state.get("heard") or self.refusal is not None:
            self._waiting("joining")  # a cluster this node knows of, whose head does not answer
            return RETRY
        named = {str(a.get("node_id")) for a in answers} | {self.node_id}
        chosen = self.config.cluster_head or (min(named) if everyone else None)
        if chosen == self.node_id:
            await self._make()
            return 0.0
        self._waiting("no_cluster")
        return RETRY

    def _observe(self, answers: list[dict[str, Any]]) -> None:
        """Terms heard in hellos' answers: a higher one taken (fencing a head that was away)."""
        terms = [a.get("term") for a in answers if a.get("cluster_id") == self.state.get("cluster_id")]
        self.raft.observe(max((t for t in terms if isinstance(t, int)), default=0))
        self._reconcile()

    async def _resume(self) -> bool:
        """A head restarted (Pro, Team): it leads again in its term unless a member it reaches knows a higher one."""
        answers, _ = await self._survey()
        self._observe(answers)
        if self.state.get("head") != self.node_id:
            return False
        self.raft.resume(self.clock())
        await self._lead("restarted")
        return True

    async def _fence(self) -> None:
        """Without HA, a head with members it is not connected to asks each who it is: a higher term deposes it."""
        if self.ha() or all(m.node_id == self.node_id or m.node_id in self.links for m in self.members()):
            return  # with HA a head out of touch steps down by itself, and a candidate's term would only disrupt
        answers, _ = await self._survey()
        self._observe(answers)

    def _waiting(self, status: str) -> None:
        if self.status != "ready" and self.status != status and self.refusal is None:
            self.status = status
            self._identify()

    def _hello(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": "node",
            "cluster_id": self.state.get("cluster_id"),
            "address": self.address,
            "version": self.version,
            "term": self.raft.term,
        }

    async def _survey(self) -> tuple[list[dict[str, Any]], bool]:
        """Every address's hello answered, and whether every address answered."""
        addresses = await self.discovery.addresses()
        found = await asyncio.gather(*(self._ask(a) for a in addresses))
        answers = [a for a in found if a is not None]
        return answers, bool(addresses) and len(answers) == len(addresses)

    async def _ask(self, address: str) -> dict[str, Any] | None:
        assert self.session is not None and self.credentials is not None  # noqa: S101  (opened first)
        try:
            channel, answered, reader = await dial(self.session, self.credentials, address, self._hello())
        except ClusterError as err:
            log.debug("cluster: no answer", extra={"address": address, "code": err.code})
            return None
        reader.cancel()
        await channel.close()
        return answered

    async def _join(self, head_address: str) -> float:
        assert self.session is not None and self.credentials is not None  # noqa: S101  (opened first)
        box: dict[str, Channel] = {}
        try:
            channel, answered, reader = await dial(
                self.session, self.credentials, head_address, self._hello(), lambda m: self._pushed(m, box)
            )
        except ClusterError:
            self._waiting("joining")
            return RETRY
        if answered.get("role") != "head":
            reader.cancel()
            await channel.close()
            self._waiting("joining")
            return RETRY
        box["head"] = channel
        asked, self.asked = self.asked, False
        try:
            welcome = await channel.request(
                "join",
                address=self.address,
                version=self.version,
                instance=self.state["instance"],
                cluster_id=self.state.get("cluster_id"),
                asked=asked,
                applied_seq=self.replication.applied,
                term=self.raft.term,
            )
        except ClusterError as err:
            reader.cancel()
            await channel.close()
            if err.code in REFUSALS:
                self.attempted.set()
                self._refuse(err.code, err.message)
                return REJOIN
            self.asked = self.asked or asked  # not now (a membership change in progress): asked again as it was
            self._waiting("joining")
            return RETRY
        self.raft.observe(int(welcome["term"]))
        self.refusal = None
        self.state.update(cluster_id=welcome["cluster_id"], head=welcome["head"], head_address=welcome["head_address"])
        self.state.pop("heard", None)
        self._save()
        self.told = welcome
        self.replication.synced = False  # not ready until it has applied what the head has
        self.role, self.status = "follower", "ready"
        self.head_channel, self.following = channel, (str(welcome["head"]), int(welcome["term"]))
        self._reconcile()  # a term heard meanwhile: this head is no longer the one to follow
        self._identify()
        self.attempted.set()
        log.info("cluster: joined", extra={"head": welcome["head"], "members": len(welcome["members"])})
        await self._follow(channel, reader)
        return RETRY

    async def _follow(self, channel: Channel, reader: asyncio.Task[None]) -> None:
        """Ping the head until the connection drops; reads go on being served meanwhile."""
        self.head_channel = channel
        try:
            while not reader.done():
                self.told = await channel.request("ping", applied_seq=self.replication.applied, usage=self.taken())
                await asyncio.wait({reader}, timeout=PING)
        except ClusterError as err:
            log.debug("cluster: the head's connection ended", extra={"code": err.code})
        finally:
            reader.cancel()
            await channel.close()
            self.head_channel, self.following = None, None
        if self.refusal is None:
            log.warning("cluster: lost the head; asking again", extra={"head": self.state.get("head")})

    def _pushed(self, msg: dict[str, Any], box: dict[str, Channel]) -> None:
        kind = msg.get("type")
        if kind == "removed":
            self._refuse(str(msg.get("code") or "E_CLUSTER_FULL"), str(msg.get("message") or "Removed by the head."))
        elif kind in ("append", "snapshot", "forward") and "head" in box:
            self._spawn(self._answer_head(box["head"], msg))

    def _spawn(self, work: Awaitable[None]) -> None:
        task: asyncio.Task[None] = asyncio.ensure_future(work)
        self.telling.add(task)
        task.add_done_callback(self.telling.discard)

    async def _answer_head(self, channel: Channel, msg: dict[str, Any]) -> None:
        """What the head asks of a follower: entries to take, a snapshot to install, a forward of a run held here."""
        try:
            if msg["type"] == "append":
                reply = answer(msg, **await self.replication.receive(msg))
            elif msg["type"] == "snapshot":
                reply = answer(msg, **await self.replication.install(msg))
            else:
                reply = answer(msg, **await self._local(msg))
        except RrotorError as err:
            reply = failed(msg, err)
        except (KeyError, TypeError, ValueError):
            reply = failed(msg, ClusterError("Not a request this node can read.", code="E_CLUSTER_MESSAGE"))
        except OSError as err:  # its log could not be written (a full disk): the head asks again
            reply = failed(msg, ClusterError(f"This node could not keep the entries: {err}", code="E_CLUSTER_APPLY"))
        with contextlib.suppress(Exception):
            await channel.send(reply)

    def _refuse(self, code: str, message: str) -> None:
        self.refusal = {"code": code, "message": message, "at": stamp(self.wall())}
        self.role, self.status = "standalone", "refused"
        self._identify()
        log.error("cluster: this node is refused", extra={"code": code, "reason": message})

    # Raft's changes, as the node acts on them

    def _reconcile(self) -> None:
        """After anything Raft heard: a head that is no longer one steps down; a follower whose head is no longer
        the one it follows lets it go (and finds the new one)."""
        raft = self.raft
        if self.role == "head" and raft.role != "head":
            self._step_down()
        following = self.following
        if (
            self.head_channel is not None
            and following is not None
            and (raft.term > following[1] or raft.head not in (None, following[0]))
        ):
            self.following = None
            self._spawn(self.head_channel.close())
        if raft.role == "head" and self.role != "head" and not self.promoting:
            self.wake.set()  # elected: the membership loop leads

    def _step_down(self) -> None:
        reason = "deposed" if self.raft.term > self.led else "no_majority"
        log.warning("cluster: this node is no longer the head", extra={"term": self.raft.term, "reason": reason})
        self.role = "follower"
        self.replication.saved(self.replication.applied)
        self._identify()
        self.audit.emit("cluster.head", who=NODE, node=self.node_id, term=self.raft.term, reason=reason)
        self._spawn(self._resign())

    async def _resign(self) -> None:
        await self.replication.stop()
        for channel in list(self.links.values()):
            await channel.close()
        self.links.clear()
        await self.deposed()

    def _member_applied(self, entry: dict[str, Any]) -> None:
        """A `member` entry applied on a follower: who left (and may come back only when asked) is kept too."""
        left = set(self.state.get("left") or [])
        if entry.get("op") == "remove" and entry.get("left"):
            left.add(str(entry.get("node")))
        elif entry.get("op") == "add":
            left.discard(str(entry.get("node")))
        self.state["left"] = sorted(left)
        self._save()

    # Elections (HA)

    async def _elections(self) -> None:
        """With HA, Raft's clock: a voter whose head has been silent for its election timeout stands; a head out
        of touch with a majority of voters steps down before anyone else can be elected."""
        while True:
            await asyncio.sleep(TICK)
            if not self.ha() or not self.state.get("cluster_id") or self.refusal is not None:
                continue
            if self.replication.lock.locked():  # taking the head's entries or snapshot: it has a head
                self.raft.deadline = max(self.raft.deadline, self.clock() + self.raft.timing.election_min)
                continue
            if self.raft.tick(self.clock()) == "campaign" and self.campaigning is None:
                self.campaigning = asyncio.create_task(self._campaign())
            self._reconcile()

    async def _campaign(self) -> None:
        """One election: the next term, this node's own vote, and every other voter asked for its own."""
        try:
            asked = self.raft.campaign(self.clock())
            self._reconcile()
            members = {m.node_id: m for m in self.members()}
            within = self.raft.timing.election_min
            voters = [members[n] for n in self.raft.quorum() if n != self.node_id and n in members]
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.gather(*(self._vote(m, asked, within) for m in voters)), within)
        finally:
            self.campaigning = None

    async def committed(self, term: int) -> None:
        """A write done on this head in `term`, answered once it is committed: at once without HA (on its disk);
        with HA once a majority of voters hold it, else E_NOT_COMMITTED after COMMIT_WITHIN (the entry is kept
        and committed when they come back). E_NO_HEAD when this node stopped being the head meanwhile."""
        seq = self.replication.log.last
        for _ in range(int(COMMIT_WITHIN / 0.02) + 1):
            if self.role != "head" or self.raft.term != term:
                raise no_head("This node stopped being the head during the call: what it wrote may be lost.")
            if self.raft.commit >= seq:
                return
            await asyncio.sleep(0.02)
        raise Unavailable(
            "Most voters do not hold this write yet: it is kept, and committed when they come back.",
            code="E_NOT_COMMITTED",
        )

    # The head

    def _me(self) -> dict[str, Any]:
        return Member(self.node_id, self.address, self.version, self.state["instance"], stamp(self.wall())).to_json()

    async def _make(self) -> None:
        self.state.update(cluster_id=str(uuid.uuid4()), left=[])
        self.state.pop("heard", None)
        self.raft.bootstrap(self._me(), self.clock())
        log.info("cluster: made, this node its head", extra={"cluster_id": self.state["cluster_id"]})
        await self._lead("bootstrap")

    async def _lead(self, reason: str, who: Who = NODE) -> None:
        """Raft made this node head: what its log holds applied first (a follower promoted), then it leads."""
        async with self.leading:
            if self.role != "head" and await self.replication.own() and self.raft.role == "head":
                self._took(reason, who)

    def _took(self, reason: str, who: Who) -> None:
        for m in self.members():
            self.seen.setdefault(m.node_id, self.wall())
        self.state.update(head=self.node_id, head_address=self.address)
        self._save()
        self.role, self.status, self.refusal, self.led = "head", "ready", None, self.raft.term
        self._identify()
        self.replication.lead()
        if reason not in ("restarted", "resumed"):
            log.info("cluster: this node is the head", extra={"term": self.raft.term, "reason": reason})
            self.audit.emit("cluster.head", who=who, node=self.node_id, term=self.raft.term, reason=reason)

    def _refresh(self) -> None:
        """Each member's address, version and instance kept as it last said them (the head's own too), and a
        learner made a voter while there are fewer than five: a change at a time, each once the last is held."""
        if not self.raft.ready_for_change():
            return
        self.said[self.node_id] = {"address": self.address, "version": self.version, "instance": self.state["instance"]}
        for m in self.raft.members:
            said = self.said.get(str(m["node_id"]))
            if said is not None and any(m.get(k) != v for k, v in said.items()):
                self.raft.add({**m, **said}, self.clock())
                break
        else:
            self.raft.fill()
        self.replication.wake()

    def told_view(self) -> dict[str, Any]:
        """What the head tells a node it admits, and each follower at its every ping."""
        return {
            "cluster_id": self.state["cluster_id"],
            "term": self.raft.term,
            "head": self.node_id,
            "head_address": self.address,
            "commit_seq": self.replication.commit_seq,
            "members": self._members_view(),
            "refused": list(self.refused),
        }

    def _refused(self, node: str, code: str, message: str) -> ClusterError:
        self.refused = [r for r in self.refused if r["node_id"] != node][-(REFUSED_KEPT - 1) :]
        self.refused.append({"node_id": node, "at": stamp(self.wall()), "code": code})
        log.warning("cluster: a node was refused", extra={"node": node, "code": code})
        self.audit.emit("cluster.refused", who=NODE, node=node, code=code, term=self.raft.term)
        return ClusterError(message, code=code)

    def _busy(self) -> ClusterError:
        return ClusterError(
            "A membership change is being committed; asked again in a moment.", code="E_CLUSTER_BUSY", status=409
        )

    def _admit(self, channel: Channel, peer: dict[str, Any], msg: dict[str, Any]) -> dict[str, Any]:
        term = msg.get("term")
        if isinstance(term, int) and not isinstance(term, bool):
            self.raft.observe(term)
            self._reconcile()
        if self.role != "head":
            raise ClusterError("This node is not the head.", code="E_NOT_HEAD")
        node, instance = str(peer["node_id"]), str(msg.get("instance") or "")
        members = self.members()
        existing = next((m for m in members if m.node_id == node), None)
        left = list(self.state.get("left") or [])
        if msg.get("cluster_id") not in (None, self.state["cluster_id"]):
            raise self._refused(node, "E_CLUSTER_ID", "This node belongs to another cluster.")
        live = existing is not None and existing.instance != instance and node in self.links
        if node == self.node_id or live:
            raise self._refused(node, "E_CLUSTER_NODE_ID", f"Another node in the cluster is already {node}.")
        address, version = str(msg.get("address") or ""), str(msg.get("version") or "")
        if existing is None:
            if node in left and not msg.get("asked"):
                raise self._refused(
                    node, "E_CLUSTER_LEFT", "This node left the cluster: rrotord cluster join on it asks again."
                )
            cap = self.licence.limits().max_nodes
            if cap is not None and len(members) >= cap:
                raise self._refused(
                    node,
                    "E_CLUSTER_FULL",
                    f"The cluster has {len(members)} members, the licence key's max_nodes ({cap}).",
                )
            if not self.raft.ready_for_change():
                raise self._busy()
            self.raft.add(Member(node, address, version, instance, stamp(self.wall())).to_json(), self.clock())
            self.replication.wake()
            self.state["left"] = [n for n in left if n != node]
            self._save()
            log.info("cluster: a node joined", extra={"node": node, "members": len(members) + 1})
            self.audit.emit("cluster.join", who=NODE, node=node, term=self.raft.term, members=len(members) + 1)
        self.said[node] = {"address": address, "version": version, "instance": instance}
        self.links[node], peer["member"], self.seen[node] = channel, node, self.wall()
        self.refused = [r for r in self.refused if r["node_id"] != node]
        self.replication.attach(node, channel, msg.get("applied_seq"))
        return self.told_view()

    def _ping(self, peer: dict[str, Any], msg: dict[str, Any]) -> dict[str, Any]:
        node = peer.get("member")
        members = [m.node_id for m in self.members()]
        if self.role != "head" or node not in members:
            raise ClusterError("Not a member: join first.", code="E_CLUSTER_MEMBER")
        self.seen[str(node)] = self.wall()
        self.replication.heard_from(str(node), msg.get("applied_seq"))
        self.replication.trim([m for m in members if m != self.node_id])
        usage = msg.get("usage")
        if isinstance(usage, list) and usage:
            self.usage([u for u in usage if isinstance(u, dict)])
        return self.told_view()

    def _remove(self, node: str, code: str, reason: str, who: Who) -> dict[str, Any]:
        if node == self.node_id:
            raise ClusterError(
                "The head cannot leave the cluster: promote another node first (rrotord cluster promote <node>).",
                code="E_CLUSTER_HEAD",
            )
        members = self.members()
        if node not in {m.node_id for m in members}:
            raise ClusterError(f"{node} is not a member of the cluster.", code="E_CLUSTER_MEMBER", status=404)
        if not self.raft.ready_for_change():
            raise self._busy()
        self.raft.remove(node, **({"left": True} if code == "E_CLUSTER_LEFT" else {}))
        self.replication.wake()
        if code == "E_CLUSTER_LEFT":
            self.state["left"] = sorted({*(self.state.get("left") or []), node})
            self._save()
        self.seen.pop(node, None)
        messages = {
            "E_CLUSTER_LEFT": "This node was removed from the cluster: rrotord cluster join on it asks again.",
            "E_CLUSTER_FULL": "The licence key's max_nodes is below the cluster's size: the last joined are removed.",
        }
        channel = self.links.pop(node, None)
        self.replication.detach(node)
        if channel is not None:
            self._spawn(self._tell_removed(channel, code, messages.get(code, "Removed from the cluster.")))
        log.warning("cluster: a node was removed", extra={"node": node, "reason": reason})
        self.audit.emit("cluster.leave", who=who, node=node, reason=reason, term=self.raft.term)
        return {"removed": node, "members": len(members) - 1}

    async def _tell_removed(self, channel: Channel, code: str, message: str) -> None:
        with contextlib.suppress(Exception):
            await channel.send(frame("removed", code=code, message=message))
        await channel.close()

    def _fit(self) -> None:
        """A key with a smaller max_nodes: the members joined last go (never the head) until the cluster fits,
        one change at a time."""
        limits = self.licence.limits()
        if not limits.cluster_sync_enabled:
            self.changed.set()  # the key no longer allows a cluster at all: the gate closes it, nobody is cut
            return
        cap = limits.max_nodes
        members = self.members()
        if cap is not None and len(members) > cap and self.raft.ready_for_change():
            last = next(m for m in reversed(members) if m.node_id != self.node_id)
            self._remove(last.node_id, "E_CLUSTER_FULL", "max_nodes", NODE)
            self._refused(last.node_id, "E_CLUSTER_FULL", "")

    def _evict(self) -> None:
        """A member unseen for RROTOR_CLUSTER_EVICT_AFTER is removed; it may join again when it is back."""
        now = self.wall()
        for m in self.members():
            if m.node_id == self.node_id or m.node_id in self.links or not self.raft.ready_for_change():
                continue
            if now - self.seen.get(m.node_id, now) > self.config.cluster_evict_after:
                self._remove(m.node_id, "E_CLUSTER_EVICTED", "unseen", NODE)

    # The listener

    async def _accept(self, request: web.Request) -> web.WebSocketResponse:
        transport = request.transport
        assert transport is not None  # noqa: S101  (a request being served has its connection)
        name = peer_name(transport.get_extra_info("ssl_object"))
        ws = web.WebSocketResponse(autoping=True, max_msg_size=MESSAGE_MAX)
        await ws.prepare(request)
        channel, peer = Channel(ws, name), dict[str, Any]()
        self.accepted.add(channel)
        try:
            async for msg in channel.frames():
                if msg["type"] == "forward" and peer.get("member"):
                    self._spawn(self._reply(channel, msg, self._forwarded(msg)))  # a run: never in the loop's way
                    continue
                await self._reply(channel, msg, self._serve(channel, peer, msg))
        finally:
            self.accepted.discard(channel)
            node = peer.get("member")
            if node is not None and self.links.get(node) is channel:
                del self.links[node]
                self.replication.detach(node)
        return ws

    async def _reply(self, channel: Channel, msg: dict[str, Any], work: Awaitable[dict[str, Any]]) -> None:
        try:
            reply = await work
        except RrotorError as err:
            reply = failed(msg, err)
        with contextlib.suppress(Exception):
            await channel.send(reply)

    async def _serve(self, channel: Channel, peer: dict[str, Any], msg: dict[str, Any]) -> dict[str, Any]:
        kind = msg["type"]
        if kind == "hello":
            return answer(msg, **self._greet(channel, peer, msg))
        if not peer:
            raise ClusterError("Say hello first.", code="E_CLUSTER_HELLO")
        if kind == "join" and peer["kind"] == "node":
            return answer(msg, **self._admit(channel, peer, msg))
        if kind == "ping" and peer["kind"] == "node":
            return answer(msg, **self._ping(peer, msg))
        if kind == "vote" and peer["kind"] == "node":
            if msg.get("candidate") != peer["node_id"] or not all(
                type(msg.get(k)) is int for k in ("term", "last_seq", "last_term")
            ):
                raise ClusterError(
                    "A vote names its term, its candidate (the asker) and its log.", code="E_CLUSTER_MESSAGE"
                )
            got = self.raft.on_vote(msg, self.clock())
            self._reconcile()
            return answer(msg, **got)
        if kind == "command":
            return answer(msg, **await self._command(peer, msg))
        if kind == "telemetry" and peer.get("member"):
            self._fenced(msg.get("term"))
            return answer(msg, **await self.on_telemetry(str(peer["member"]), msg))
        raise ClusterError(f"There is no message {kind!r} here.", code="E_CLUSTER_MESSAGE")

    def _fenced(self, term: Any) -> None:
        """A member's traffic to the head checked like the rest: a lower term refused (E_CLUSTER_TERM), a higher one
        taken (this head deposed: E_NOT_HEAD, and the sender finds the new head)."""
        if type(term) is not int or term < self.raft.term:
            raise ClusterError("Of another term than this node's.", code="E_CLUSTER_TERM", term=self.raft.term)
        self.raft.observe(term)
        self._reconcile()

    def _greet(self, channel: Channel, peer: dict[str, Any], msg: dict[str, Any]) -> dict[str, Any]:
        """The hello: the node it names must be the one its certificate names (or `rrotord cluster`'s own)."""
        node, kind = msg.get("node_id"), msg.get("kind")
        named = COMMAND if kind == "command" else node
        if kind not in ("node", "command") or not isinstance(node, str) or channel.peer != named:
            log.warning("cluster: peer certificate names another node", extra={"node": node, "named": channel.peer})
            raise ClusterError("The hello names another node than the certificate does.", code="E_CLUSTER_PEER")
        if kind == "node" and not NODE_ID.match(node):
            raise ClusterError("A node id is 1 to 63 of a-z, 0-9 and -.", code="E_CLUSTER_PEER")
        peer.update(node_id=node, kind=kind)
        return {
            "node_id": self.node_id,
            "role": self.role,
            "status": self.status,
            "cluster_id": self.state.get("cluster_id"),
            "head": self.state.get("head"),
            "head_address": self.state.get("head_address") if self.state.get("cluster_id") else None,
            "term": self.raft.term,
            "last_seq": self.replication.log.last,
            "last_term": self.raft.last_term,
            "applied_seq": self.replication.applied,
            "version": self.version,
        }

    async def _command(self, peer: dict[str, Any], msg: dict[str, Any]) -> dict[str, Any]:
        """`rrotord cluster join | leave <node> | promote <node>` (a leave passed on to the head, a promotion to the
        node named)."""
        name, node = msg.get("name"), msg.get("node")
        if name == "promote":
            if not isinstance(node, str) or not NODE_ID.match(node):
                raise BadRequest("Name the node to make head: rrotord cluster promote <node id>.")
            return await self._promote(node, bool(msg.get("force")), bool(msg.get("yes")))
        if name == "leave":
            if not isinstance(node, str) or not NODE_ID.match(node):
                raise BadRequest("Name the node to remove: rrotord cluster leave <node id>.")
            if self.role == "head":
                for _ in range(int(CHANGE_WITHIN / 0.02)):  # the last change committed first
                    if self.raft.ready_for_change():
                        return self._remove(node, "E_CLUSTER_LEFT", "left", OPERATOR)
                    await asyncio.sleep(0.02)
                raise self._busy()
            if self.head_channel is not None:
                got = await self.head_channel.request("command", name="leave", node=node)
                return {k: v for k, v in got.items() if k not in ("type", "id", "ok")}
            raise ClusterError("No head answers this node: try again on another, or once it is back.", code="E_NO_HEAD")
        if name == "join" and peer["kind"] == "command":
            if self.role == "standalone":
                self.asked = True
                self.attempted.clear()
                self.wake.set()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.attempted.wait(), ASKED_WITHIN)
            return {"node_id": self.node_id, "role": self.role, "status": self.status, "refusal": self.refusal}
        raise BadRequest("The commands are join, leave <node id> and promote <node id>.")

    # Manual failover

    async def _promote(self, node: str, force: bool, yes: bool) -> dict[str, Any]:
        """`rrotord cluster promote <node>`: done by the node named (passed on to it from any other)."""
        members = {m.node_id: m for m in self.members()}
        if node not in members:
            raise ClusterError(f"{node} is not a member of the cluster.", code="E_CLUSTER_MEMBER", status=404)
        if node != self.node_id:
            assert self.session is not None and self.credentials is not None  # noqa: S101  (opened first)
            channel, _, reader = await dial(self.session, self.credentials, members[node].address, self._hello())
            try:
                got = await channel.request("command", name="promote", node=node, force=force, yes=yes, within=60.0)
            finally:
                reader.cancel()
                await channel.close()
            return {k: v for k, v in got.items() if k not in ("type", "id", "ok")}
        if self.role == "head":
            return {"head": self.node_id, "term": self.raft.term, "promoted": False, "reason": "already the head"}
        if self.ha() and (force or node not in self.raft.voters()):
            raise ClusterError(
                "This cluster elects its head (HA): only a voter with a log as up to date as most voters' can be "
                "promoted, and --force, which could lose acknowledged writes, is refused.",
                code="E_CLUSTER_HA",
                status=409,
            )
        others = [m for m in members.values() if m.node_id != self.node_id]
        found = await asyncio.gather(*(self._ask(m.address) for m in others))
        reached = {
            m.node_id: a
            for m, a in zip(others, found, strict=True)
            if a is not None and a.get("node_id") == m.node_id and a.get("cluster_id") == self.state.get("cluster_id")
        }
        top = max([self.replication.log.last, *(int(a.get("last_seq") or 0) for a in reached.values())])
        lags = {
            m: {
                "reachable": m == self.node_id or m in reached,
                "last_seq": self.replication.log.last if m == self.node_id else (reached.get(m) or {}).get("last_seq"),
                "lag": top - (self.replication.log.last if m == self.node_id else int(reached[m].get("last_seq") or 0))
                if m == self.node_id or m in reached
                else None,
            }
            for m in members
        }
        quorum = self.raft.quorum()
        if len([n for n in quorum if n == self.node_id or n in reached]) < majority(len(quorum)):
            raise ClusterError(
                "A majority of the members must be reachable from the node promoted.",
                code="E_CLUSTER_QUORUM",
                status=409,
                members=lags,
            )
        mine = (self.raft.last_term, self.replication.log.last)
        if not force and any(
            (int(a.get("last_term") or 0), int(a.get("last_seq") or 0)) > mine for a in reached.values()
        ):
            raise ClusterError(
                f"{node} is behind another member: the writes it lacks would be lost. Promote the member with "
                "no lag, or add --force to lose them.",
                code="E_CLUSTER_LAG",
                status=409,
                members=lags,
            )
        head = self.state.get("head")
        if isinstance(head, str) and head != self.node_id and head not in reached and not yes:
            raise ClusterError(
                f"The head ({head}) does not answer. Make sure it is down (or cut off from every node) before "
                "another node is made head, then confirm.",
                code="E_CLUSTER_CONFIRM",
                status=409,
                head=head,
                members=lags,
            )
        self._observe(list(reached.values()))
        self.promoting = True
        try:
            asked = self.raft.campaign(self.clock(), promote=True, force=force)
            self._reconcile()
            await asyncio.gather(*(self._vote(members[n], asked) for n in quorum if n in reached))
            if self.raft.role != "head":
                self.raft.abandon()
                raise ClusterError(
                    "The members did not give this node their votes (another promotion, or a member with a longer "
                    "log). The term has moved and a head that answered may have stepped down: run it again.",
                    code="E_CLUSTER_VOTES",
                    status=409,
                    members=lags,
                )
            await self._lead("promoted", OPERATOR)
        finally:
            self.promoting = False
        return {"head": self.node_id, "term": self.raft.term, "promoted": True, "forced": force, "members": lags}

    async def _vote(self, member: Member, asked: dict[str, Any], within: float = VOTE_WITHIN) -> None:
        """A vote asked of one member, on a connection of its own; its answer given to Raft."""
        assert self.session is not None and self.credentials is not None  # noqa: S101  (opened first)
        try:
            channel, _, reader = await dial(self.session, self.credentials, member.address, self._hello())
        except ClusterError:
            return
        try:
            body = {k: v for k, v in asked.items() if k != "type"}
            got = await channel.request("vote", within=within, **body)
        except (ClusterError, TimeoutError):
            return
        finally:
            reader.cancel()
            await channel.close()
        self.raft.voted_for(member.node_id, asked, got, self.clock())
        self._reconcile()

    # Replication's requests on the listener

    def _member_asking(self, request: web.Request) -> None:
        transport = request.transport
        name = peer_name(transport.get_extra_info("ssl_object")) if transport is not None else None
        if self.role != "head" or name not in {m.node_id for m in self.members()}:
            raise web.HTTPForbidden(text="members only, from the head")

    async def _send_file(self, request: web.Request, path: Path, headers: dict[str, str]) -> web.StreamResponse:
        try:
            res = web.StreamResponse(headers={"Content-Type": "application/x-tar", **headers})
            res.content_length = path.stat().st_size
            await res.prepare(request)
            with path.open("rb") as f:
                while chunk := f.read(1024 * 1024):
                    await res.write(chunk)
            await res.write_eof()
            return res
        finally:
            path.unlink(missing_ok=True)

    async def _version(self, request: web.Request) -> web.StreamResponse:
        """A version's `src/` and `venv/`, the head's build, with the head's architecture."""
        self._member_asking(request)
        model, commit = request.match_info["model"], request.match_info["commit"]
        try:
            tar = await asyncio.to_thread(self.replication.version_tar, model, commit)
        except RrotorError as err:
            return web.json_response(err.to_json(), status=err.status)
        return await self._send_file(request, tar, {"Rrotor-Arch": self.replication.arch})

    async def _snapshot(self, request: web.Request) -> web.StreamResponse:
        self._member_asking(request)
        tar, seq = await self.replication.snapshot()
        return await self._send_file(request, tar, {"Rrotor-Cluster-Seq": str(seq)})

    async def _fetch(self, path: str, dest: Path) -> dict[str, str]:
        """A follower's GET on its head's listener."""
        address = self.state.get("head_address")
        if self.session is None or self.credentials is None or not isinstance(address, str):
            raise ClusterError("No head to fetch from.", code="E_NO_HEAD")
        from rrotord.cluster.replication import SNAPSHOT_WITHIN

        return await fetch(self.session, self.credentials, address, path, dest, SNAPSHOT_WITHIN)

    # Forwarding

    async def forward(
        self, op: str, args: dict[str, Any], target: str | None = None, within: float = FORWARD_WITHIN
    ) -> dict[str, Any]:
        """A request done by another node, as the caller in hand: the head (`target` None), or the node that holds a
        run. Answers what it answered (and the seq it stood at, `seq`); raises what it refused."""
        who = WHO.get()
        msg = {"op": op, "args": args, "target": target, "deadline": within, "term": self.raft.term}
        if who is not None:
            msg["who"] = {"actor": who.actor, "client": who.client, "via": who.via}
        if target == self.node_id or (target is None and self.role == "head"):
            return await self._local(msg)
        if self.role == "head":
            return await self._relay(str(target), msg)
        channel = self.head_channel
        if channel is None or self.role != "follower":
            raise no_head()
        try:
            got = await channel.request("forward", within=within, **msg)
        except ClusterError as err:
            if err.code in ("E_NO_HEAD", "E_CLUSTER_CLOSED"):
                raise no_head("The head went while this request was with it: writes wait for the next.") from None
            raise
        return {k: v for k, v in got.items() if k not in ("type", "id", "ok")}

    async def _relay(self, target: str, msg: dict[str, Any]) -> dict[str, Any]:
        channel = self.links.get(target)
        if channel is None:
            raise RrotorError(
                f"The node that holds this run ({target}) is not in the cluster now: the run is lost.",
                code="E_RUN_LOST",
                status=410,
            )
        deadline = msg.get("deadline")
        got = await channel.request("forward", within=deadline if isinstance(deadline, float | int) else None, **msg)
        return {k: v for k, v in got.items() if k not in ("type", "id", "ok")}

    async def _forwarded(self, msg: dict[str, Any]) -> dict[str, Any]:
        """A follower's forward at the head: done here, or passed to the node it names."""
        target, term = msg.get("target"), msg.get("term")
        if isinstance(term, int) and term > self.raft.term:  # the follower knows a newer head: this one is deposed
            self.raft.observe(term)
            self._reconcile()
        if self.role != "head":
            raise no_head("This node is no longer the head.")
        body = {k: msg.get(k) for k in ("op", "args", "who", "deadline")}
        if isinstance(target, str) and target != self.node_id:
            return answer(msg, **await self._relay(target, {**body, "target": target}))
        return answer(msg, **await self._local(body))

    async def _local(self, msg: dict[str, Any]) -> dict[str, Any]:
        """A forwarded request done on this node, as its caller (who, from where, by which route)."""
        if self.serve_forward is None:
            raise no_head("This node does not serve forwarded requests.")
        sent = msg.get("who")
        token = WHO.set(
            Who(dict(sent.get("actor") or {}), sent.get("client"), sent.get("via")) if isinstance(sent, dict) else None
        )
        try:
            done = await self.serve_forward({"op": msg.get("op"), "args": msg.get("args") or {}})
        finally:
            WHO.reset(token)
        return {"result": done, "seq": self.replication.log.last if self.role == "head" else None}

    async def caught_up(self, seq: int, within: float = READ_YOUR_WRITES) -> bool:
        """Whether this node has applied `seq`, waiting up to `within` seconds for it (read-your-writes)."""
        for _ in range(int(within / 0.02) + 1):
            if self.role != "follower" or self.replication.applied >= seq:
                return True
            await asyncio.sleep(0.02)
        return False
