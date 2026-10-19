"""The cluster's audit store and a follower's way to it, without a cluster: a follower sends its kept events from a
cursor and the head takes each once, whatever is sent again; a head behind the follower (a new head, a lost store)
gets everything again; below Enterprise nothing is kept; what a head refuses; a follower's drain buffering while it
has no head; the forward and commit-wait spans and the trace carried across the hop."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import rrotor
from rrotord import aggregate
from rrotord.aggregate import Aggregate, ClusterAudit, exact
from rrotord.api import forward
from rrotord.audit import Audit
from rrotord.cluster.transport import ClusterError
from rrotord.config import Config
from rrotord.drain import Drain, Head, SendFailed
from rrotord.errors import BadRequest
from rrotord.otel import encoded, log_record, resource

Tier = list[str]


class Nowhere:
    """A collector that takes nothing, at once."""

    async def send(self, signal: str, body: bytes) -> None:
        raise SendFailed("nowhere")

    async def close(self) -> None:
        pass


def store(root: Path, node: str, tier: Tier) -> Audit:
    return Audit(root / node / "audit", node, tier=lambda: tier[0])


class Link:
    """A follower's connection to a head, in this process: each request answered by the head's `received`."""

    def __init__(self, head: Aggregate, node: str = "f") -> None:
        self.head, self.node = head, node
        self.fail: Exception | None = None

    async def request(self, kind: str, **fields: Any) -> dict[str, Any]:
        if self.fail is not None:
            raise self.fail
        return await self.head.received(self.node, {"type": kind, **fields})


def pair(tmp_path: Path, tiers: tuple[Tier, Tier]) -> tuple[Aggregate, Aggregate, Link]:
    own = store(tmp_path, "h", tiers[0])
    head = Aggregate(own, ClusterAudit(own.folder / "cluster", own), cast(Any, SimpleNamespace(role="head")))
    link = Link(head)
    mine = store(tmp_path, "f", tiers[1])
    follower = Aggregate(
        mine,
        ClusterAudit(mine.folder / "cluster", mine),
        cast(Any, SimpleNamespace(role="follower", head_channel=link, raft=SimpleNamespace(term=1))),
    )
    return head, follower, link


def held(head: Aggregate) -> list[tuple[str, int]]:
    return sorted((e["node_id"], e["n"]) for e in head.store.query({"kind": "run.call", "limit": 1000})["events"])


async def test_a_follower_sends_from_its_cursor_and_the_head_takes_each_event_once(tmp_path):
    tiers: tuple[Tier, Tier] = (["enterprise"], ["enterprise"])
    head, follower, link = pair(tmp_path, tiers)
    for n in range(3):
        follower.audit.emit("run.call", n=n)
    head.audit.emit("run.call", n=9)
    assert await follower.round() and await head.round()
    assert held(head) == [("f", 0), ("f", 1), ("f", 2), ("h", 9)]
    assert follower.cursor.exists() and await follower.round()  # nothing new: nothing sent twice

    follower.cursor.unlink()  # the cursor lost: everything sent again, taken once
    follower.audit.emit("run.call", n=3)
    assert await follower.round()
    assert held(head) == [("f", 0), ("f", 1), ("f", 2), ("f", 3), ("h", 9)]

    for path in head.store.folder.iterdir():  # a head with none of it (a new one): the follower goes back
        path.unlink()
    follower.audit.emit("run.call", n=4)
    assert await follower.round()
    assert held(head) == [("f", n) for n in range(5)]

    tiers[0][0] = "pro"  # a head that keeps nothing takes nothing: the follower holds on to its cursor
    follower.audit.emit("run.call", n=5)
    assert await follower.round() is False
    head.store.catch_up()  # below Enterprise: nothing copied
    tiers[0][0] = "enterprise"

    link.fail = ClusterError("gone", code="E_CLUSTER_CLOSED")
    assert await follower.round() is False
    link.fail = None
    follower.cluster.head_channel = None
    assert await follower.round() is False
    follower.cluster.head_channel = link  # type: ignore[assignment]
    assert await follower.round() and held(head)[-1] == ("f", 5)

    tiers[1][0] = "pro"
    assert await follower.round()  # nothing kept here: nothing to send
    tiers[1][0] = "enterprise"
    follower.cluster.role = "standalone"
    assert await follower.round()


async def test_what_the_head_refuses(tmp_path):
    head, _, _ = pair(tmp_path, (["enterprise"], ["enterprise"]))
    bad: list[dict[str, Any]] = [
        {"signal": "audit", "events": "x"},
        {"signal": "audit", "events": [["d", 1]]},
        {"signal": "audit", "events": [["d", "1", {}]]},
        {"signal": "audit", "events": [], "since": ["d", True]},
        {"signal": "nope", "to": [], "body": ""},
        {"signal": "logs", "to": "collector", "body": ""},
        {"signal": "logs", "to": [], "body": 1},
    ]
    for msg in bad:
        with pytest.raises(BadRequest):
            await head.received("f", msg)
    assert await head.received("f", {"signal": "logs", "to": ["collector"], "body": ""}) == {"taken": 0}  # no drain
    head.cluster.role = "follower"
    with pytest.raises(ClusterError) as err:
        await head.received("f", {"signal": "audit", "events": []})
    assert err.value.code == "E_NOT_HEAD"
    with pytest.raises(BadRequest):
        exact(["d"])


def config(tmp_path: Path, **kw: Any) -> Config:
    base = Config(
        tmp_path / "data",
        "self",
        "http://127.0.0.1:9",
        0,
        0,
        None,
        Path(rrotor.__file__).parent,
        tmp_path / "run",
        node_id="h",
        otel=(("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9"),),
    )
    return dataclasses.replace(base, **kw)


def a_line(text: str) -> logging.LogRecord:
    return logging.makeLogRecord({"name": "rrotor.t", "msg": text, "levelname": "INFO", "levelno": 20})


async def test_a_head_buffers_what_it_is_sent_for_the_destinations_it_has(tmp_path):
    own = Audit(tmp_path / "audit", "h")
    head = Aggregate(own, ClusterAudit(own.folder / "cluster", own), cast(Any, SimpleNamespace(role="head")))
    head.drain = Drain(config(tmp_path), runtime_id=None, place=lambda: ("c1", "head"), collector=Nowhere())
    res = resource({}, node_id="f", runtime_id=None, mode="self")
    record = log_record(a_line("from f"), {"message": "from f"}, res)
    body = encoded("logs", [record], False)
    sent = {"signal": "logs", "to": ["collector", "glyphh", "elsewhere"], "body": base64.b64encode(body).decode()}
    assert await head.received("f", sent) == {"taken": 1}  # no Glyphh here: only the collector's
    assert head.drain.collector is not None and len(head.drain.collector.buffers["logs"]) == 1
    for broken in ("not base64!", base64.b64encode(b"\xff\xff\xff").decode()):
        with pytest.raises(BadRequest):
            await head.received("f", {**sent, "body": broken})
    await head.drain.stop()


async def test_the_loop_survives_a_bad_round(tmp_path, caplog, monkeypatch):
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    own = Audit(tmp_path / "audit", "h")
    a = Aggregate(own, ClusterAudit(own.folder / "cluster", own), cast(Any, SimpleNamespace(role="head")), sleep)

    async def broken() -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(a, "round", broken)
    with caplog.at_level(logging.ERROR, logger="rrotor.drain"), pytest.raises(asyncio.CancelledError):
        await a.run()
    assert sleeps == [aggregate.EVERY, aggregate.EVERY]
    assert [r.getMessage() for r in caplog.records] == ["sending audit events to the head failed"]


class Upstream:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail: Exception | None = None

    async def request(self, kind: str, **fields: Any) -> dict[str, Any]:
        if self.fail is not None:
            raise self.fail
        self.sent.append({"type": kind, **fields})
        return {"taken": 1}


async def test_a_follower_without_its_head_buffers_drops_its_oldest_and_sends_when_it_can(tmp_path):
    place = ["follower"]
    channel: list[Upstream | None] = [None]
    now = [100.0]
    d = Drain(
        config(tmp_path, cluster=True, drain_buffer=3, drain_signals=("logs",)),
        runtime_id=None,
        place=lambda: ("c1", place[0]),
        upstream=lambda: channel[0],
        clock=lambda: now[0],
        collector=Nowhere(),
    )
    assert d.head is not None and d.collector is not None
    for n in range(5):
        d.handler.emit(a_line(f"line {n}"))
    await d.tick()
    view = d.view()["drains"]["logs"]
    assert (view["state"], view["error"], view["dropped"], view["via"]) == ("failing", "no head", 2, "head")
    channel[0] = up = Upstream()
    up.fail = ClusterError("closed", code="E_CLUSTER_CLOSED")
    now[0] += 60
    await d.tick()
    assert d.view()["drains"]["logs"]["error"] == "the head did not take the batch: E_CLUSTER_CLOSED"
    up.fail = TimeoutError()
    now[0] += 60
    await d.tick()
    assert d.view()["drains"]["logs"]["error"] == "the head did not take the batch: TimeoutError"
    up.fail = None
    now[0] += 60
    await d.tick()
    assert [m["to"] for m in up.sent] == [["collector"]] and d.view()["drains"]["logs"]["state"] == "ok"
    assert len(d.collector.buffers["logs"]) == 0  # nothing for the collector itself: the head sends it

    place[0] = "head"  # the head reads the cluster's store when it has one, else its own
    assert d._store("collector", ".cursor") is None  # no audit store given at all
    await d.stop()
    with pytest.raises(SendFailed, match="no head"):
        await Head(lambda: None, {}).send("logs", b"")


async def test_a_head_without_the_clusters_store_drains_its_own(tmp_path):
    own = Audit(tmp_path / "audit", "h")
    d = Drain(config(tmp_path), runtime_id=None, audit=own, place=lambda: ("c1", "head"), collector=Nowhere())
    assert d._store("collector", ".cursor") == (own, own.folder / ".cursor")
    no_upward = Drain(
        config(tmp_path, cluster=True, drain_at="node"), runtime_id=None, upstream=lambda: None, collector=Nowhere()
    )
    assert no_upward.head is None  # each node exports itself, Glyphh off: nothing for the head
    await d.stop()
    await no_upward.stop()


class Spans:
    """What a forward needs of the services: a cluster that answers, a drain whose spans are read."""

    def __init__(self, tmp_path: Path, seq: Any) -> None:
        self.drain = Drain(config(tmp_path, drain_signals=("traces",)), runtime_id=None, collector=Nowhere())
        self.forwarded: list[dict[str, Any]] = []
        self.runner = SimpleNamespace(approvals=lambda: [])

        async def go(op: str, args: dict[str, Any], target: str | None = None) -> dict[str, Any]:
            self.forwarded.append(args)
            return {"result": {"done": True}, "seq": seq}

        async def caught_up(seq: int) -> bool:
            return True

        self.cluster = SimpleNamespace(forward=go, caught_up=caught_up)

    def spans(self) -> list[Any]:
        assert self.drain.collector is not None
        return list(self.drain.collector.buffers["traces"].items)


async def test_a_forward_and_its_commit_wait_are_spans_and_the_trace_goes_with_it(tmp_path):
    s = Spans(tmp_path, 7)
    with s.drain.telemetry.span("GET /v1/runs/{run_id}"):
        assert await forward.relayed(cast(Any, s), "status", {"run_id": "n0~x"}, "n0") == {"done": True}
    names = [x.name for x in s.spans()]
    assert names == ["rrotor.cluster.forward", "rrotor.cluster.commit_wait", "GET /v1/runs/{run_id}"]
    carried = s.forwarded[0]["trace"]
    assert carried["traceparent"].split("-")[1] == format(s.spans()[0].context.trace_id, "032x")
    assert await forward.serve(cast(Any, s), {"op": "approvals_here", "args": {"trace": carried}}) == {"approvals": []}
    there = s.spans()[-1]
    assert there.name == "rrotor.cluster.forwarded" and there.context.trace_id == s.spans()[0].context.trace_id
    await s.drain.stop()
    plain = Spans(tmp_path, None)
    assert await forward.relayed(cast(Any, plain), "status", {"run_id": "x"}) == {"done": True}
    assert "trace" in plain.forwarded[0] and [x.name for x in plain.spans()] == ["rrotor.cluster.forward"]
    await plain.drain.stop()
