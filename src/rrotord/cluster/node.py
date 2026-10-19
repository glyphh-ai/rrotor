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
run on it. The head cannot leave: moving the head is `rrotord cluster promote` (manual failover, not built yet).

**Replication.** The head ships its log to each follower it admits, from what the follower says it applied (a
snapshot first for a new node or one behind the log), over the follower's own connection: `append` and `snapshot`
are the head's requests, answered with the seq applied. A joined follower is `catching_up` (not ready) until it has
applied what the head had committed, and again once behind it for more than a second. `GET /cluster/v1/versions/
<model>/<commit>.tar` and `GET /cluster/v1/snapshot` are plain requests on the listener, for members only.

**Forwarding** (`forward`): a follower sends a writing call, a deploy, and anything about a run another node holds
to its head, with the caller as data (who, from where, by which route) and a deadline; the head does it (as that
caller), or passes it to the follower that holds the run (`E_RUN_LOST`, 410, when that node is not connected), and
answers with the seq its log stands at. A follower with no head answers `E_NO_HEAD` (503). What a forward does is
the API's own operations (`rrotord/api/forward.py`, `serve_forward`). Each ping of a follower carries its applied
seq and its runtime keys' calls since the last, which the head adds to its own `usage`.

**Not built yet:** terms past 1, fencing and promotion (manual failover), elections and majority commit (`ha` is
false), `member` entries (membership is kept in `cluster/state.json`), and the followers' telemetry to the head."""

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
VOTERS = 5  # the first five members by join (election: not built yet)
REFUSED_KEPT = 50
FORWARD_WITHIN = 3630.0  # a forwarded request's answer: a run's longest seconds (3600) and its end
READ_YOUR_WRITES = 2.0  # a node waits this long to have applied a seq it was asked to be at
REFUSALS = frozenset({"E_CLUSTER_FULL", "E_CLUSTER_NODE_ID", "E_CLUSTER_LEFT", "E_CLUSTER_ID"})
NODE = Who({"type": "rrotor"}, None, "cluster")
OPERATOR = Who({"type": "local"}, None, "cluster")  # `rrotord cluster ...`, on a node, with the cluster secret


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
    ) -> None:
        self.config, self.licence, self.audit, self.version, self.wall = config, licence, audit, version, wall
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
        self.replication = Replication(
            config.data / "cluster" / "log",
            config.run_dir / "cluster" / "scratch",
            config.cluster_log_keep,
            models or Models(config.data),
            self.access,
            licence,
            role=lambda: self.role,
            term=lambda: int(self.state.get("term") or 1),
            saved=self._applied,
            applied=int(self.state.get("applied_seq") or 0),
            wall=wall,
        )
        self.replication.fetch = self._fetch
        # what a forwarded request does: the API's operations, set by rrotord/api/app.py
        self.serve_forward: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
        self.usage: Callable[[list[dict[str, Any]]], None] = lambda _: None  # the head: a follower's key calls
        self.taken: Callable[[], list[dict[str, Any]]] = list  # a follower: its key calls since the last ping
        self.roled: Callable[[], None] = lambda: None  # this node's role changed (the link looks again)

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

    def members(self) -> list[Member]:
        found = (Member.of(m) for m in self.state.get("members") or [])
        return [m for m in found if m is not None]

    def _members_view(self) -> list[dict[str, Any]]:
        if self.role != "head":
            return list(self.told.get("members") or [])
        now, head = self.wall(), self.state.get("head")
        return [
            {
                "node_id": m.node_id,
                "address": m.address,
                "role": "head" if m.node_id == head else "follower",
                "voter": i < VOTERS,
                "applied_seq": self._applied_by(m.node_id),
                "lag": max(self.replication.log.last - self._applied_by(m.node_id), 0),
                "last_seen": stamp(now if m.node_id == self.node_id else self.seen.get(m.node_id, now)),
                "connected": m.node_id == self.node_id or m.node_id in self.links,
                "version": m.version,
                "instance": m.instance,
                "joined_at": m.joined_at,
            }
            for i, m in enumerate(self.members())
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
            "term": self.state.get("term"),
            "commit_seq": self.replication.commit_seq if self.member else 0,
            "applied_seq": self.replication.applied_seq if self.member else 0,
            "tier": limits.tier,
            "max_nodes": limits.max_nodes,
            "ha": False,  # elected heads: not built yet
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
            "term": self.state.get("term"),
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
        if self.membership is not None:
            self.membership.cancel()
            await asyncio.gather(self.membership, return_exceptions=True)
            self.membership = None
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
        if self.state.get("cluster_id") and self.state.get("head") == self.node_id:
            self._lead()
            self._fit()
            self._evict()
            return HEAD_EVERY
        if self.refusal is not None and self.refusal["code"] == "E_CLUSTER_LEFT" and not self.asked:
            return REJOIN  # it left: it asks again only when `rrotord cluster join` says so
        answers, everyone = await self._survey()
        heads = sorted(answers, key=lambda a: a.get("role") != "head")
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
            self._make()
            return 0.0
        self._waiting("no_cluster")
        return RETRY

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
            )
        except ClusterError as err:
            reader.cancel()
            await channel.close()
            self.attempted.set()
            if err.code in REFUSALS:
                self._refuse(err.code, err.message)
                return REJOIN
            self._waiting("joining")
            return RETRY
        self.refusal = None
        self.state.update(
            cluster_id=welcome["cluster_id"],
            term=welcome["term"],
            head=welcome["head"],
            head_address=welcome["head_address"],
            members=welcome["members"],
        )
        self._save()
        self.told = welcome
        self.replication.synced = False  # not ready until it has applied what the head has
        self.role, self.status = "follower", "ready"
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
                told = await channel.request("ping", applied_seq=self.replication.applied, usage=self.taken())
                self.told = told
                if names(told.get("members")) != names(self.state.get("members")):
                    self.state["members"] = told["members"]  # kept for a restart: who the members were
                    self._save()
                await asyncio.wait({reader}, timeout=PING)
        except ClusterError as err:
            log.debug("cluster: the head's connection ended", extra={"code": err.code})
        finally:
            reader.cancel()
            await channel.close()
            self.head_channel = None
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
        """What the head asks of a follower: entries to apply, a snapshot to install, a forward of a run held here."""
        try:
            if msg["type"] == "append":
                reply = answer(msg, applied_seq=await self.replication.receive(msg))
            elif msg["type"] == "snapshot":
                reply = answer(msg, applied_seq=await self.replication.install())
            else:
                reply = answer(msg, **await self._local(msg))
        except RrotorError as err:
            reply = failed(msg, err)
        with contextlib.suppress(Exception):
            await channel.send(reply)

    def _refuse(self, code: str, message: str) -> None:
        self.refusal = {"code": code, "message": message, "at": stamp(self.wall())}
        self.role, self.status = "standalone", "refused"
        self._identify()
        log.error("cluster: this node is refused", extra={"code": code, "reason": message})

    # The head

    def _make(self) -> None:
        self.state.update(
            cluster_id=str(uuid.uuid4()),
            term=1,
            head=self.node_id,
            head_address=self.address,
            members=[
                Member(self.node_id, self.address, self.version, self.state["instance"], stamp(self.wall())).to_json()
            ],
            left=[],
        )
        self.state.pop("heard", None)
        self._save()
        log.info("cluster: made, this node its head", extra={"cluster_id": self.state["cluster_id"]})
        self._lead()
        self.audit.emit("cluster.head", who=NODE, node=self.node_id, term=1, reason="bootstrap")

    def _lead(self) -> None:
        if self.role == "head":
            return
        members = self.members()
        for m in members:
            if m.node_id == self.node_id:
                m.address, m.version = self.address, self.version
            self.seen.setdefault(m.node_id, self.wall())
        self.state.update(members=[m.to_json() for m in members], head_address=self.address)
        self._save()
        self.role, self.status, self.refusal = "head", "ready", None
        self._identify()
        self.replication.lead()

    def told_view(self) -> dict[str, Any]:
        """What the head tells a node it admits, and each follower at its every ping."""
        return {
            "cluster_id": self.state["cluster_id"],
            "term": self.state["term"],
            "head": self.node_id,
            "head_address": self.address,
            "commit_seq": self.replication.log.last,
            "members": self._members_view(),
            "refused": list(self.refused),
        }

    def _refused(self, node: str, code: str, message: str) -> ClusterError:
        self.refused = [r for r in self.refused if r["node_id"] != node][-(REFUSED_KEPT - 1) :]
        self.refused.append({"node_id": node, "at": stamp(self.wall()), "code": code})
        log.warning("cluster: a node was refused", extra={"node": node, "code": code})
        self.audit.emit("cluster.refused", who=NODE, node=node, code=code, term=self.state.get("term"))
        return ClusterError(message, code=code)

    def _admit(self, channel: Channel, peer: dict[str, Any], msg: dict[str, Any]) -> dict[str, Any]:
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
            existing = Member(node, "", "", instance, stamp(self.wall()))
            members.append(existing)
            self.state["left"] = [n for n in left if n != node]
            log.info("cluster: a node joined", extra={"node": node, "members": len(members)})
            self.audit.emit("cluster.join", who=NODE, node=node, term=self.state["term"], members=len(members))
        existing.address, existing.version, existing.instance = (
            str(msg.get("address") or ""),
            str(msg.get("version") or ""),
            instance,
        )
        self.state["members"] = [m.to_json() for m in members]
        self._save()
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
                "The head cannot leave the cluster: another node must be made head first (rrotord cluster promote, "
                "not built yet).",
                code="E_CLUSTER_HEAD",
            )
        members = self.members()
        if node not in {m.node_id for m in members}:
            raise ClusterError(f"{node} is not a member of the cluster.", code="E_CLUSTER_MEMBER", status=404)
        members = [m for m in members if m.node_id != node]
        self.state["members"] = [m.to_json() for m in members]
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
            task = asyncio.get_running_loop().create_task(
                self._tell_removed(channel, code, messages.get(code, "Removed from the cluster."))
            )
            self.telling.add(task)
            task.add_done_callback(self.telling.discard)
        log.warning("cluster: a node was removed", extra={"node": node, "reason": reason})
        self.audit.emit("cluster.leave", who=who, node=node, reason=reason, term=self.state["term"])
        return {"removed": node, "members": len(members)}

    async def _tell_removed(self, channel: Channel, code: str, message: str) -> None:
        with contextlib.suppress(Exception):
            await channel.send(frame("removed", code=code, message=message))
        await channel.close()

    def _fit(self) -> None:
        """A key with a smaller max_nodes: the members joined last go (never the head) until the cluster fits."""
        limits = self.licence.limits()
        if not limits.cluster_sync_enabled:
            self.changed.set()  # the key no longer allows a cluster at all: the gate closes it, nobody is cut
            return
        cap = limits.max_nodes
        while cap is not None and len(members := self.members()) > cap:
            last = next(m for m in reversed(members) if m.node_id != self.node_id)
            self._remove(last.node_id, "E_CLUSTER_FULL", "max_nodes", NODE)
            self._refused(last.node_id, "E_CLUSTER_FULL", "")

    def _evict(self) -> None:
        """A member unseen for RROTOR_CLUSTER_EVICT_AFTER is removed; it may join again when it is back."""
        now = self.wall()
        for m in self.members():
            if m.node_id == self.node_id or m.node_id in self.links:
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
        if kind == "command":
            return answer(msg, **await self._command(peer, msg))
        raise ClusterError(f"There is no message {kind!r} here.", code="E_CLUSTER_MESSAGE")

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
            "term": self.state.get("term"),
            "version": self.version,
        }

    async def _command(self, peer: dict[str, Any], msg: dict[str, Any]) -> dict[str, Any]:
        """`rrotord cluster join | leave <node>` (a leave passed on by a follower too)."""
        name, node = msg.get("name"), msg.get("node")
        if name == "leave":
            if not isinstance(node, str) or not NODE_ID.match(node):
                raise BadRequest("Name the node to remove: rrotord cluster leave <node id>.")
            if self.role == "head":
                return self._remove(node, "E_CLUSTER_LEFT", "left", OPERATOR)
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
        raise BadRequest("The commands are join, and leave <node id>.")

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
        msg = {"op": op, "args": args, "target": target, "deadline": within}
        if who is not None:
            msg["who"] = {"actor": who.actor, "client": who.client, "via": who.via}
        if target == self.node_id or (target is None and self.role == "head"):
            return await self._local(msg)
        if self.role == "head":
            return await self._relay(str(target), msg)
        channel = self.head_channel
        if channel is None or self.role != "follower":
            raise Unavailable("No head answers this node: writes wait for it.", code="E_NO_HEAD")
        got = await channel.request("forward", within=within, **msg)
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
        target = msg.get("target")
        body = {k: msg.get(k) for k in ("op", "args", "who", "deadline")}
        if isinstance(target, str) and target != self.node_id:
            return answer(msg, **await self._relay(target, {**body, "target": target}))
        return answer(msg, **await self._local(body))

    async def _local(self, msg: dict[str, Any]) -> dict[str, Any]:
        """A forwarded request done on this node, as its caller (who, from where, by which route)."""
        if self.serve_forward is None:
            raise Unavailable("This node does not serve forwarded requests.", code="E_NO_HEAD")
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
