"""rrotord, put together: the sandbox checked, the runner, the link and the API, until it is told to stop; its log
lines naming this node, its audit events (kept on Enterprise), and, when one is set, its drain; with the cluster on,
its place in the cluster (rrotord/cluster/node.py), gated by the licence key, and its part in the cluster's drain and
audit store at the head (rrotord/aggregate.py)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from aiohttp import web

from rrotord import VERSION
from rrotord import log as logs
from rrotord.access import Access
from rrotord.aggregate import Aggregate, ClusterAudit
from rrotord.api.app import make
from rrotord.api.routes import Services
from rrotord.audit import SERVER, Audit, current
from rrotord.cluster.node import Cluster
from rrotord.config import Config
from rrotord.deploy import Deployer
from rrotord.drain import Drain, warning
from rrotord.errors import RrotorError
from rrotord.heartbeat import Heartbeat
from rrotord.introspect import Introspector
from rrotord.keys import public_key
from rrotord.licence import Licence
from rrotord.link import protocol as p
from rrotord.link.client import LinkClient
from rrotord.link.credentials import Credentials
from rrotord.link.handler import LinkHandler
from rrotord.link.outbox import Outbox
from rrotord.parts import Parts
from rrotord.programs import Programs
from rrotord.proxy import EgressProxy
from rrotord.runner.engine import Runner
from rrotord.runner.launch import Launcher, SandboxLauncher
from rrotord.runner.record import Records
from rrotord.sandbox.probe import Layers, container_needs, probe
from rrotord.store import read_json, write_json
from rrotord.vault import Vault

log = logging.getLogger("rrotor")
EXPIRE_EVERY = 30.0
USAGE_EVERY = 60.0
LICENCE_EVERY = 3600.0  # the licence key's file read again (and on SIGHUP)


@dataclass(slots=True)
class Daemon:
    config: Config
    layers: Layers
    launcher: Launcher = field(default_factory=SandboxLauncher)
    deployer: Deployer = field(init=False)
    parts: Parts = field(init=False)
    licence: Licence = field(init=False)
    access: Access = field(init=False)
    runner: Runner = field(init=False)
    link: LinkClient = field(init=False)
    handler: LinkHandler = field(init=False)
    heartbeat: Heartbeat | None = field(init=False)
    audit: Audit = field(init=False)
    gathered: ClusterAudit = field(init=False)
    aggregate: Aggregate = field(init=False)
    drain: Drain | None = field(init=False)
    cluster: Cluster = field(init=False)
    session: aiohttp.ClientSession | None = None

    def __post_init__(self) -> None:
        c = self.config
        logs.identify(node_id=c.node_id)
        if c.mode == "self" and self.layers.checks.get("bubblewrap", {}).get("ok") is False:
            self.layers.needs = container_needs()  # its owner runs the container: say what it lacks
            log.warning("bwrap cannot start: the container lacks", extra={"needs": self.layers.needs})
        programs = Programs(c.data)
        self.deployer = Deployer(programs, c.run_dir / "deploy")
        self.parts = Parts(c.data, public_key(c.release_key) if c.release_key else None)
        self.licence = Licence(c.data, c.runtime_id)
        self.audit = Audit(
            c.data / "audit",
            c.node_id,
            tier=lambda: self.licence.limits().tier,
            retention_days=c.audit_retention_days,
            max_mb=c.audit_max_mb,
            payloads=c.log_payloads,
        )
        self.access = Access(c.data, c.keys_sha256, on_change=self._keys_changed)
        self.cluster = Cluster(c, self.licence, self.audit, VERSION, programs=programs, access=self.access)
        self.cluster.usage, self.cluster.taken = self.access.usage.merge, self.access.usage.take
        self.licence.on_event = self._licence_event
        self.licence.start(c.licence_env)  # never fatal: a bad key is logged and the node runs as Community
        credentials = Credentials(c.data)
        if (
            c.runtime_id
            and c.runtime_token
            and (credentials.runtime_id, credentials.token) != (c.runtime_id, c.runtime_token)
        ):
            credentials.set(c.runtime_id, c.runtime_token)  # hosted and self: from the environment
        self.gathered = ClusterAudit(c.data / "audit" / "cluster", self.audit)
        self.drain = (
            Drain(
                c,
                runtime_id=credentials.runtime_id,
                audit=self.audit,
                token=lambda: credentials.token,
                place=self.cluster.identity,
                upstream=lambda: self.cluster.head_channel,
                term=lambda: self.cluster.raft.term,
                gathered=self.gathered,
            )
            if Drain.wanted(c)
            else None
        )
        self.aggregate = Aggregate(self.audit, self.gathered, self.cluster)
        self.aggregate.drain = self.drain
        self.cluster.on_telemetry = self.aggregate.received
        self.handler = handler = LinkHandler(programs, None, self.deployer, self.licence, self.access, self.audit)  # type: ignore[arg-type]
        self.runner = Runner(
            programs=programs,
            records=Records(c.data),
            proxy=EgressProxy(c.run_dir / "proxy"),
            launcher=self.launcher,
            sdk=c.sdk,
            layers=self.layers,
            notify=handler.notify,
            parts=self.parts,
            audit=self.audit,
            telemetry=self.drain.telemetry if self.drain is not None else None,
            node=lambda: c.node_id if self.cluster.member else None,
            readonly=lambda: self.cluster.role == "follower",
            vault=Vault(c.data),
        )
        self.cluster.deposed = self._deposed
        handler.runner = self.runner
        self.link = LinkClient(
            url=c.link_url,
            version=VERSION,
            mode=c.mode,
            credentials=credentials,
            outbox=Outbox(c.data / "link"),
            handler=handler,
            connect=self._connect,
            enabled=c.link,
            held=lambda: self.cluster.unlinked,
            identity=lambda: (c.node_id, *self.cluster.identity()),
        )
        self.cluster.roled = self.link.regate.set
        handler.link = self.link
        self.heartbeat = (
            Heartbeat(
                server=c.server,
                credentials=credentials,
                licence=self.licence,
                post=self._post,
                active_runs=lambda: len(self.runner.workers),
                data=c.data,
                version=VERSION,
                node_id=c.node_id,
                cluster=self.cluster.identity,
            )
            if c.mode == "self" and c.link
            else None
        )
        if self.drain is not None:
            self._gauges(self.drain)

    def _gauges(self, drain: Drain) -> None:
        """What the drain's metrics watch: the link's state, the cluster's term and each follower's lag."""
        t = drain.telemetry
        t.gauge("rrotor.link.state", lambda: [(1.0, {"state": self.link.state})])
        t.gauge("rrotor.cluster.term", self._term)
        t.gauge("rrotor.cluster.lag", self._lag)

    def _term(self) -> list[tuple[float, dict[str, str]]]:
        """The cluster's term, on a member."""
        return [(float(self.cluster.state.get("term") or 1), {})] if self.cluster.member else []

    def _lag(self) -> list[tuple[float, dict[str, str]]]:
        """On the head: each follower's seqs behind it, as it last acknowledged."""
        if self.cluster.role != "head":
            return []
        members = self.cluster.view()["members"]
        return [(float(m["lag"]), {"node": m["node_id"]}) for m in members if m["role"] == "follower"]

    async def _deposed(self) -> None:
        """This node was the cluster's head and is no longer: its writing runs in flight fail."""
        await self.runner.stop_writing("E_NO_HEAD", "This node stopped being the cluster's head during the run.")

    def _keys_changed(self, change: dict[str, Any]) -> None:
        """The runtime keys' list changed: an audit event, and on a cluster's head a `keys` entry."""
        self.audit.emit("keys.changed", who=SERVER, **change)
        self.cluster.replication.keys_changed()

    def _licence_event(self, kind: str, detail: dict[str, Any]) -> None:
        """A licence key taken, past grace or refused, as the audit log tells it: from the heartbeat or `welcome`,
        the server's doing; from `PUT /v1/licence`, its caller's; from the environment or a file, rrotor's."""
        self.audit.emit(kind, who=SERVER if detail.get("source") in ("heartbeat", "welcome") else current(), **detail)
        self.cluster.licence_changed()  # the cluster's gate looked at again

    def server_logs(self) -> None:
        """At every start: server-side logging's warning while it is on; turning it on or off is an audit event."""
        on, path = self.config.server_logs, self.config.data / "logging.json"
        if on:
            log.warning(warning(self.config.server))
        saved = read_json(path)
        before = saved.get("server_logs") if isinstance(saved, dict) else False
        if before != on:
            self.audit.emit("config.server_logs", on=on)
            write_json(path, {"server_logs": on})

    async def _fetch(self, url: str) -> bytes:
        """A part's tarball by URL (its release asset), at most 256 MB."""
        if self.session is None:
            self.session = aiohttp.ClientSession()
        async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=600)) as res:
            if res.status != 200:
                raise RrotorError(
                    f"The part could not be fetched: {res.status} from {url}.", code="E_PART_FETCH", status=502
                )
            return await res.content.read(256 * 1024 * 1024 + 1)

    async def _post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        """A JSON POST to the Cloud, answered as (status, body): the introspection of a sign-in, the heartbeat."""
        if self.session is None:
            self.session = aiohttp.ClientSession()
        async with self.session.post(url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=10)) as res:
            try:
                return res.status, await res.json()
            except (aiohttp.ContentTypeError, ValueError):
                return res.status, None

    async def _connect(self, url: str, headers: dict[str, str]) -> aiohttp.ClientWebSocketResponse:
        if self.session is None:
            self.session = aiohttp.ClientSession()
        return await self.session.ws_connect(
            url, headers=headers, autoping=True, timeout=aiohttp.ClientWSTimeout(ws_close=5)
        )

    def services(self) -> Services:
        return Services(
            self.config,
            self.runner.programs,
            self.deployer,
            self.runner,
            self.link.credentials,
            self.link,
            self.layers,
            self.parts,
            self.licence,
            self._fetch,
            self.access,
            Introspector(self.config.server, self.link.credentials, self._post)
            if self.config.public and self.config.link
            else None,
            self.handler.report,
            self.heartbeat,
            self.audit,
            self.drain,
            self.cluster,
            self.gathered,
        )

    def install_builtin(self) -> list[str]:
        """The parts the image carries (<name>-<version>-<arch>.tar.gz with its .sha256 and .sig beside it), each
        installed once, at boot. Answers what was installed now."""
        done: list[str] = []
        folder = self.config.builtin_parts
        if not folder.is_dir():
            return done
        for archive in sorted(folder.glob("*.tar.gz")):
            stem = archive.name[: -len(".tar.gz")]
            name, _, rest = stem.partition("-")
            version = rest.rsplit("-", 1)[0]
            have = self.parts.get(name)
            if have is not None and have.info.version == version:
                continue
            try:
                sha256 = (folder / f"{stem}.sha256").read_text().strip()
                signature = (folder / f"{stem}.sig").read_text().strip()
                self.parts.install(name, version, archive.read_bytes(), sha256, signature)
            except (OSError, RrotorError) as err:
                log.warning("a built-in part was not installed", extra={"part": stem, "reason": str(err)})
                continue
            done.append(stem)
        return done

    async def _expire(self) -> None:
        while True:
            await asyncio.sleep(EXPIRE_EVERY)
            try:
                await self.runner.expire()
            except Exception:
                log.exception("expiring waiting runs failed")

    async def _licence(self) -> None:
        while True:
            await asyncio.sleep(LICENCE_EVERY)
            try:
                self.licence.tick()
            except Exception:
                log.exception("reading the licence key again failed")

    def reread(self) -> None:
        """SIGHUP: the licence key's file read again now."""
        try:
            self.licence.tick()
        except Exception:
            log.exception("reading the licence key again failed")

    async def report_usage(self) -> None:
        """The runtime keys' calls since the last report, owed up the link as one numbered `usage` when there were
        any; nothing otherwise, or when there is no runtime token to owe them under. A cluster's follower sends its
        calls to its head with each ping instead, and the head's `usage` is the cluster's."""
        if self.cluster.role == "follower":
            return
        keys = self.access.usage.take()
        if not self.link.configured:
            return
        if keys:
            await self.link.owe(lambda seq: p.usage(seq, keys))

    async def _usage(self) -> None:
        while True:
            await asyncio.sleep(USAGE_EVERY)
            try:
                await self.report_usage()
            except Exception:
                log.exception("reporting usage failed")

    async def serve(self, stop: asyncio.Event) -> None:
        if self.drain is not None:
            self.drain.start()
        self.server_logs()
        for stem in self.install_builtin():
            log.info("built-in part installed", extra={"part": stem})
        await self.runner.recover()
        api = web.AppRunner(make(self.services()), access_log=None)
        await api.setup()
        await web.TCPSite(api, "0.0.0.0", self.config.local_port).start()  # noqa: S104  (published to loopback only)
        log.info("rrotor up", extra={"version": VERSION, "mode": self.config.mode, "sandbox": self.layers.ready})
        await self.cluster.start()
        tasks = [
            asyncio.create_task(self.link.run()),
            asyncio.create_task(self._expire()),
            asyncio.create_task(self._usage()),
            asyncio.create_task(self._licence()),
        ]
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGHUP, self.reread)
        if self.heartbeat is not None:
            tasks.append(asyncio.create_task(self.heartbeat.run()))
        if self.drain is not None:
            tasks.append(asyncio.create_task(self.drain.run()))
        if self.config.cluster:
            tasks.append(asyncio.create_task(self.aggregate.run()))
        await stop.wait()
        try:
            await self.report_usage()  # the last minute's calls, while the link may still carry them
        except Exception:
            log.exception("reporting usage failed")
        loop.remove_signal_handler(signal.SIGHUP)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.cluster.stop()
        await self.runner.shutdown()
        await api.cleanup()
        if self.session is not None:
            await self.session.close()
        log.info("rrotor stopped")
        if self.drain is not None:
            await self.drain.stop()  # the last lines, "stopped" with them


async def main(config: Config) -> None:  # pragma: no cover  (the process itself: exercised by the image's tests)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    layers = await probe()
    if not layers.ready:
        log.warning("sandbox not ready: runs are refused", extra={"why": layers.why()})
    daemon = Daemon(config, layers)
    with contextlib.suppress(asyncio.CancelledError):
        await daemon.serve(stop)
