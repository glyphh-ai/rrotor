"""The aggregate drain (docs/specs/cluster.md, build order step 9): three daemons in one event loop and a fake
collector. One endpoint sees every node's logs, metrics, traces and audit events, each with its node's identity; the
cluster's audit store at the head holds every member's events once, and GET /v1/audit on a follower reads it; with
RROTOR_DRAIN_AT=node each node exports itself, while server-side logging still goes from the head only."""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from rrotord.api.app import make
from rrotord.audit import Who
from rrotord.cluster.transport import ClusterError
from rrotord.daemon import Daemon
from tests.daemon.cluster.conftest import Nodes, node_config, settled, until
from tests.fakes import FakeCollector, LocalLauncher, keys, ready

KEY = "rk_" + "k" * 30
HOST = "rrotor.acme.com"
IDS = ["n0", "n1", "n2"]


class Fleet:
    def __init__(self, nodes: Nodes, url: str) -> None:
        self.nodes, self.url = nodes, url
        self.daemons, self.clients = list[Daemon](), list[TestClient[Any, Any]]()

    async def add(self, node_id: str, ids: list[str], licence: str, **extra: Any) -> tuple[Daemon, Any]:
        c = node_config(
            self.nodes.root,
            node_id,
            self.nodes.port(node_id),
            self.nodes.peers(ids),
            runtime_id=str(uuid.uuid4()),
            runtime_token="tok_" + "t" * 20,
            public_host=HOST,
            keys_sha256=(hashlib.sha256(KEY.encode()).hexdigest(),),
            licence_env=(("GLYPHH_LICENSE_KEY", licence),),
            cluster_head="n0",
            otel=(("OTEL_EXPORTER_OTLP_ENDPOINT", self.url),),
            **extra,
        )
        d = Daemon(dataclasses.replace(c, server=self.url), ready(), launcher=LocalLauncher())
        self.daemons.append(d)
        client = TestClient(TestServer(make(d.services())))
        await client.start_server()
        self.clients.append(client)
        await d.cluster.start()
        return d, client

    async def three(self, **extra: Any) -> list[Daemon]:
        key = keys().key(tier="enterprise", max_nodes=3)
        for node in IDS:
            await self.add(node, IDS, key, **extra)
        await until(lambda: settled(self.daemons[0].cluster, [d.cluster for d in self.daemons[1:]]))
        return self.daemons

    async def close(self) -> None:
        for d in self.daemons:
            if d.drain is not None:
                await d.drain.stop()
            await d.cluster.stop()
        for client in self.clients:
            await client.close()


@pytest.fixture
async def fake() -> AsyncIterator[FakeCollector]:
    f = FakeCollector()
    f.url = await f.start()  # type: ignore[attr-defined]
    yield f
    await f.close()


@pytest.fixture
async def fleet(nodes: Nodes, fake: FakeCollector) -> AsyncIterator[Fleet]:
    made = Fleet(nodes, fake.url)  # type: ignore[attr-defined]
    yield made
    await made.close()


def line(d: Daemon, text: str) -> None:
    """A log line of this daemon's (each daemon's drain is fed its own: one process holds three nodes)."""
    assert d.drain is not None
    d.drain.handler.emit(logging.makeLogRecord({"name": "rrotor.t", "msg": text, "levelname": "INFO", "levelno": 20}))


def signals(d: Daemon, n: int) -> None:
    assert d.drain is not None
    node = d.config.node_id
    line(d, f"hello from {node} {n}")
    with d.drain.telemetry.span(f"work on {node} {n}"):
        pass
    d.audit.emit("run.call", who=Who({"type": "key", "key_sha256": "ab" * 32}, "10.0.4.17", "rest"), program="m", n=n)


async def rounds(daemons: list[Daemon]) -> None:
    """The followers send, then the head copies its own and exports."""
    for d in daemons[1:]:
        assert d.drain is not None
        await d.drain.tick()
        assert await d.aggregate.round()
    head = daemons[0]
    assert head.drain is not None
    assert await head.aggregate.round()
    await head.drain.tick()


async def get(client: Any, path: str) -> tuple[int, Any]:
    res = await client.get(path, headers={"Host": HOST, "Authorization": f"Bearer {KEY}"})
    return res.status, await res.json()


async def test_one_endpoint_sees_every_nodes_signals_with_each_nodes_identity(fleet: Fleet, fake: FakeCollector):
    daemons = await fleet.three()
    head, f1, f2 = daemons
    cluster_id = head.cluster.cluster_id
    for d in daemons:
        signals(d, 1)
    await rounds(daemons)
    assert {f"hello from {n} 1" for n in IDS} <= {r["body"] for r in fake.logs() if not r["event_name"]}
    assert {f"work on {n} 1" for n in IDS} <= {s["name"] for s in fake.spans()}
    identities = {
        (r["service.instance.id"], r["rrotor.node.role"], r.get("rrotor.cluster.id")) for r in fake.resources()
    }
    assert identities == {("n0", "head", cluster_id), ("n1", "follower", cluster_id), ("n2", "follower", cluster_id)}
    calls = [r for r in fake.logs() if r["event_name"] == "rrotor.audit.run.call"]
    assert sorted(r["node_id"] for r in calls) == IDS and {r["client"] for r in calls} == {"10.0.4.17"}
    assert {"rrotor.cluster.term", "rrotor.cluster.lag", "rrotor.drain.dropped"} <= fake.metrics()
    for d in (f1, f2):  # nothing went from a follower to the collector: all of it by the head
        assert d.drain is not None and d.drain.collector is not None and d.drain.head is not None
        assert {v["state"] for v in d.drain.collector.view().values()} <= {"waiting", "off"}
        assert d.drain.head.view()["logs"]["state"] == "ok"
    _, health = await get(fleet.clients[1], "/v1/health")
    assert health["drains"]["logs"]["via"] == "head" and health["drains"]["logs"]["state"] == "ok"

    status, page = await get(fleet.clients[1], "/v1/audit?kind=run.call")
    assert status == 200 and sorted(e["node_id"] for e in page["events"]) == IDS
    status, page = await get(fleet.clients[0], "/v1/audit?kind=cluster.join")
    assert status == 200 and sorted(e["node"] for e in page["events"]) == ["n1", "n2"]

    (f1.audit.folder / ".cursor-head").unlink()  # the follower lost its cursor: what it sends again is taken once
    signals(f1, 2)
    fake.got["collector"].clear()
    await rounds(daemons)
    kept = head.gathered.query({"kind": "run.call", "limit": 100})["events"]
    assert sorted((e["node_id"], e["n"]) for e in kept) == [("n0", 1), ("n1", 1), ("n1", 2), ("n2", 1)]
    assert [(r["node_id"], r["n"]) for r in fake.logs() if r["event_name"] == "rrotor.audit.run.call"] == [("n1", 2)]


async def test_a_new_heads_store_gets_every_followers_events_again(fleet: Fleet):
    daemons = await fleet.three()
    head, f1, _ = daemons
    signals(f1, 1)
    await rounds(daemons)
    for path in head.gathered.folder.iterdir():  # a head with an empty store, as a new one would be
        path.unlink()
    signals(f1, 2)
    await rounds(daemons)
    kept = head.gathered.query({"kind": "run.call", "limit": 100})["events"]
    assert sorted((e["node_id"], e["n"]) for e in kept) == [("n1", 1), ("n1", 2)]


async def test_drain_at_node_exports_each_node_itself_and_server_logs_still_go_from_the_head(
    fleet: Fleet, fake: FakeCollector
):
    daemons = await fleet.three(drain_at="node", server_logs=True)
    head, f1, _ = daemons
    for d in daemons:
        signals(d, 1)
    for d in daemons:
        assert d.drain is not None
        await d.drain.tick()
        assert await d.aggregate.round()
    assert head.drain is not None
    await head.drain.tick()
    customer = fake.logs()
    assert sorted(r["node_id"] for r in customer if r["event_name"] == "rrotor.audit.run.call") == IDS  # once each
    assert {f"hello from {n} 1" for n in IDS} <= {r["body"] for r in customer}
    glyphh = fake.logs("glyphh")
    assert {f"hello from {n} 1" for n in IDS} <= {r["body"] for r in glyphh}
    assert sorted(r["node_id"] for r in glyphh if r["event_name"] == "rrotor.audit.run.call") == IDS
    assert b"10.0.4.17" not in fake.raw("glyphh") and b"10.0.4.17" in fake.raw("collector")
    assert {r["service.instance.id"] for r in fake.resources("glyphh")} == set(IDS)
    assert f1.drain is not None and f1.drain.glyphh is not None
    assert {v["state"] for v in f1.drain.glyphh.view().values()} == {"waiting"}  # a follower never sends to Glyphh
    _, health = await get(fleet.clients[1], "/v1/health")
    assert health["drains"]["logs"]["state"] == "ok" and "via" not in health["drains"]["logs"]
    assert health["server_logs"]["signals"]["logs"]["via"] == "head"


async def test_a_node_without_the_aggregate_takes_no_telemetry(nodes: Nodes):
    head = nodes.make("a", ["a", "b"], cluster_head="a")
    follower = nodes.make("b", ["a", "b"], cluster_head="a")
    await head.start()
    await follower.start()
    await until(lambda: follower.role == "follower" and follower.head_channel is not None)
    assert follower.head_channel is not None
    with pytest.raises(ClusterError) as err:
        await follower.head_channel.request("telemetry", signal="logs", term=follower.raft.term)
    assert err.value.code == "E_CLUSTER_MESSAGE"


async def test_telemetry_is_fenced_and_the_drain_follows_the_head(fleet: Fleet, fake: FakeCollector):
    daemons = await fleet.three()
    old, f1, f2 = daemons
    assert f1.cluster.head_channel is not None
    for term in (None, old.cluster.raft.term - 1):  # no term, or a lower one: refused
        with pytest.raises(ClusterError) as err:
            await f1.cluster.head_channel.request("telemetry", signal="logs", term=term)
        assert err.value.code == "E_CLUSTER_TERM"
    done = await f1.cluster._command({"kind": "command"}, {"name": "promote", "node": "n1"})  # the head moved
    assert done["head"] == "n1"
    await until(lambda: old.cluster.role == "follower" and old.cluster.head_channel is not None)
    await until(lambda: f2.cluster.head_channel is not None and f2.cluster.state.get("head") == "n1")
    fake.got["collector"].clear()
    for d in daemons:
        signals(d, 7)
    await rounds([f1, old, f2])  # the deposed head sends to the new one, which exports for all
    assert {f"hello from {n} 7" for n in IDS} <= {r["body"] for r in fake.logs() if not r["event_name"]}
    roles = {(r["service.instance.id"], r["rrotor.node.role"]) for r in fake.resources()}
    assert ("n1", "head") in roles and ("n0", "follower") in roles and ("n0", "head") not in roles
    assert old.drain is not None and old.drain.collector is not None
    assert {v["state"] for v in old.drain.collector.view().values()} <= {"waiting", "off"}  # it exports no more
    assert old.cluster.head_channel is not None
    with pytest.raises(ClusterError) as err:  # a frame of a later term than the head's deposes it
        await old.cluster.head_channel.request("telemetry", signal="logs", term=f1.cluster.raft.term + 1)
    assert err.value.code == "E_NOT_HEAD" and f1.cluster.role == "follower"
