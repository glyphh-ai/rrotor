"""ctx.graph (rrotor/graph.py): what the manifest allows, records shaped into nodes and edges, writes sent a batch at
a time, the graph's refusals raised with their code; and the in-memory graph a program's own tests run against
(rrotor.testing.FakeGraph), through the Harness."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import ModuleType
from typing import Any

import pytest

from rrotor import Graph, GraphError, Result
from rrotor.graph import BATCH, batches, plain, shape
from rrotor.testing import FakeGraph, Harness

ACME = {"type": "account", "key": "acme"}
DEALS: list[dict[str, Any]] = [
    {"id": 1, "name": "Renewal", "notes": "Closed in March.", "stage": "won", "account_id": "acme", "owner": None},
    {"id": 2, "name": "Expansion", "notes": "", "stage": "open", "account_id": "", "tags": ["a", "b"]},
    {"id": None, "name": "No key"},
]


class Recorder:
    def __init__(self, answer: Any = None) -> None:
        self.asked: list[dict[str, Any]] = []
        self.answer = answer

    def __call__(self, ask: dict[str, Any]) -> dict[str, Any]:
        self.asked.append(ask)
        if self.answer is not None:
            answer: dict[str, Any] = self.answer
            return answer
        return {"status": "done", "data": {"nodes": len(ask.get("nodes", [])), "edges": len(ask.get("edges", []))}}


def test_a_manifest_that_does_not_name_the_graph_is_refused_and_one_that_reads_cannot_write():
    with pytest.raises(GraphError) as undeclared:
        Graph().schema()
    assert undeclared.value.code == "E_GRAPH_UNDECLARED" and '"graph": "read"' in undeclared.value.message
    reader = Graph("read", Recorder({"status": "done", "data": {"hits": []}}))
    assert reader.search("x") == []
    for write in (reader.upsert, reader.delete):
        with pytest.raises(GraphError) as readonly:
            write(nodes=[ACME])
        assert readonly.value.code == "E_GRAPH_READONLY"
    with pytest.raises(GraphError, match="No graph is kept here"):
        Graph("read").schema()


def test_records_are_shaped_into_nodes_and_edges():
    nodes, edges = shape(
        DEALS,
        type="deal",
        key="id",
        title="name",
        text=["notes"],
        links={"account_id": ("belongs_to", "account"), "tags": ("tagged", "tag"), "owner": ("owned_by", "person")},
    )
    assert nodes == [
        {"type": "deal", "key": "1", "props": {"stage": "won"}, "title": "Renewal", "text": "Closed in March."},
        {"type": "deal", "key": "2", "props": {"stage": "open"}, "title": "Expansion", "text": ""},
    ]
    one, two = {"type": "deal", "key": "1"}, {"type": "deal", "key": "2"}
    assert edges == [
        {"from": one, "rel": "belongs_to", "to": ACME},
        {"from": two, "rel": "tagged", "to": {"type": "tag", "key": "a"}},
        {"from": two, "rel": "tagged", "to": {"type": "tag", "key": "b"}},
    ]
    nodes, edges = shape(
        [{"slug": "x", "at": dt.date(2026, 11, 18), "cost": Decimal("1.5"), "account": "acme"}],
        type="event",
        key=lambda row: f"e-{row['slug']}",
        title=lambda row: row["slug"].upper(),
        props=iter(["at", "cost", "missing"]),
        links={"account": ("has_event", "account", "in")},
    )
    assert nodes == [{"type": "event", "key": "e-x", "props": {"at": "2026-11-18", "cost": "1.5"}, "title": "X"}]
    assert edges == [{"from": ACME, "rel": "has_event", "to": {"type": "event", "key": "e-x"}}]


def test_values_json_cannot_carry_become_text():
    when = dt.datetime(2026, 11, 18, 9, 0)
    assert plain({1: when, "set": {3}, "t": (1, 2.5, True, None)}) == {
        "1": "2026-11-18 09:00:00",
        "set": [3],
        "t": [1, 2.5, True, None],
    }


def test_writes_go_a_batch_at_a_time_nodes_first(monkeypatch):
    ask = Recorder()
    graph = Graph("write", ask)
    nodes = ({"type": "n", "key": str(i)} for i in range(BATCH + 3))
    assert graph.upsert(nodes, [{"from": ACME, "rel": "r", "to": ACME}]) == {"nodes": BATCH + 3, "edges": 1}
    assert [(a["graph"], len(a.get("nodes", [])), len(a.get("edges", []))) for a in ask.asked] == [
        ("upsert", BATCH, 0),
        ("upsert", 3, 0),
        ("upsert", 0, 1),
    ]
    monkeypatch.setattr("rrotor.graph.BATCH_BYTES", 100)
    big = [{"type": "n", "key": "a", "text": "x" * 80}, {"type": "n", "key": "b", "text": "y" * 80}]
    assert [len(b) for b in batches(big)] == [1, 1] and list(batches([])) == []
    ask.asked.clear()
    assert graph.rows(DEALS, type="deal", key="id", links={"account_id": ("belongs_to", "account")}) == {
        "nodes": 2,
        "edges": 1,
    }
    assert graph.delete(nodes=[ACME], edges=[{"from": ACME, "rel": "r", "to": ACME}]) == {"nodes": 1, "edges": 1}
    assert [a["graph"] for a in ask.asked] == ["upsert", "upsert", "upsert", "delete", "delete"]
    assert "nodes" in ask.asked[-1] and "edges" in ask.asked[-2]  # edges go first: a node takes its own with it


def test_reads_send_what_was_asked_and_a_refusal_is_raised_with_its_code():
    ask = Recorder({"status": "done", "data": {"hits": [{"key": "a"}], "nodes": [{"key": "b"}], "edges": []}})
    graph = Graph("read", ask)
    assert graph.search("renewal", types=("deal",), limit=3, expand=True) == [{"key": "a"}]
    assert graph.nodes("deal", where={"stage": "won"}, limit=5, offset=2) == [{"key": "b"}]
    assert graph.nodes("deal") == [{"key": "b"}]
    assert graph.neighbours("account", 7, rel="belongs_to", direction="in", depth=2)["edges"] == []
    assert graph.node("account", 7) is not None and graph.schema()["edges"] == []
    assert ask.asked == [
        {"graph": "search", "query": "renewal", "types": ["deal"], "limit": 3, "expand": True},
        {"graph": "nodes", "type": "deal", "where": {"stage": "won"}, "limit": 5, "offset": 2},
        {"graph": "nodes", "type": "deal", "limit": 100, "offset": 0},
        {
            "graph": "neighbours",
            "type": "account",
            "key": "7",
            "rel": "belongs_to",
            "direction": "in",
            "depth": 2,
            "limit": 100,
        },
        {"graph": "node", "type": "account", "key": "7"},
        {"graph": "schema"},
    ]
    missing = Graph("read", Recorder({"status": "failed", "data": {"code": "E_GRAPH_NODE", "reason": "none"}}))
    assert missing.node("account", "nope") is None
    with pytest.raises(GraphError) as bad:
        Graph("read", Recorder({"status": "failed", "data": {"code": "E_GRAPH", "reason": "query is"}})).node("a", "b")
    assert (bad.value.code, bad.value.message) == ("E_GRAPH", "query is")
    with pytest.raises(GraphError, match="did not answer") as silent:
        Graph("read", Recorder({"status": "failed", "data": None})).schema()
    assert silent.value.code == "E_GRAPH"


def program(**functions: Any) -> ModuleType:
    module = ModuleType("program")
    for name, fn in functions.items():
        setattr(module, name, fn)
    return module


def test_a_programs_tests_run_against_a_graph_in_memory(tmp_path):
    def sync(ctx, inputs):
        wrote = ctx.graph.rows(
            DEALS,
            type="deal",
            key="id",
            title="name",
            text=["notes"],
            links={"account_id": ("belongs_to", "account")},
        )
        ctx.graph.upsert(
            nodes=[{**ACME, "title": "Acme Corp", "props": {"tier": "gold"}}],
            edges=[{"from": ACME, "rel": "in_region", "to": {"type": "region", "key": "emea"}, "props": {"w": 1}}],
        )
        return Result.done(wrote)

    def read(ctx, inputs):
        g = ctx.graph
        return Result.done(
            {
                "hits": [(h["key"], h["score"]) for h in g.search("renewal march acme")],
                "typed": [h["key"] for h in g.search("acme", types=["account"], limit=1, expand=True)],
                "links": g.search("renewal", expand=True)[0]["out"][0]["key"],
                "node": g.node("account", "acme"),
                "none": g.node("account", "nope"),
                "won": [n["key"] for n in g.nodes("deal", where={"stage": "won"})],
                "paged": [n["key"] for n in g.nodes("deal", limit=1, offset=1)],
                "near": g.neighbours("deal", 1, depth=2),
                "out": [n["key"] for n in g.neighbours("account", "acme", direction="out")["nodes"]],
                "by_rel": [n["key"] for n in g.neighbours("account", "acme", rel="in_region")["nodes"]],
                "schema": g.schema(),
            }
        )

    def lost(ctx, inputs):
        return Result.done(ctx.graph.neighbours("account", "nope"))

    def clear(ctx, inputs):
        return Result.done(
            ctx.graph.delete(
                nodes=[ACME, {"type": "account", "key": "nope"}],
                edges=[
                    {"from": {"type": "deal", "key": "1"}, "rel": "belongs_to", "to": ACME},
                    {"from": ACME, "rel": "x", "to": ACME},
                ],
            )
        )

    fake = FakeGraph()
    h = Harness(program(sync=sync, read=read, lost=lost, clear=clear), tmp_path, graph=fake)
    assert h.call("sync").data == {"nodes": 2, "edges": 1}
    assert fake.nodes[("deal", "1")] == {"title": "Renewal", "props": {"stage": "won"}, "text": "Closed in March."}
    assert fake.edges[(("account", "acme"), "in_region", ("region", "emea"))] == {"w": 1}
    got = h.call("read").data
    assert got["hits"] == [("1", 2), ("acme", 1)] and got["typed"] == ["acme"] and got["links"] == "acme"
    assert (got["node"]["title"], got["node"]["in_total"], got["node"]["out"][0]["edge"]) == ("Acme Corp", 1, {"w": 1})
    assert (got["none"], got["won"], got["paged"]) == (None, ["1"], ["2"])
    assert {(n["key"], n["distance"]) for n in got["near"]["nodes"]} == {("1", 0), ("acme", 1), ("emea", 2)}
    assert {"from": {"type": "deal", "key": "1"}, "rel": "belongs_to", "to": ACME} in got["near"]["edges"]
    assert (got["out"], got["by_rel"]) == (["acme", "emea"], ["acme", "emea"])
    assert got["schema"]["types"][0] == {"type": "account", "count": 1, "props": ["tier"], "sources": []}
    assert {"from": "deal", "rel": "belongs_to", "to": "account", "count": 1} in got["schema"]["relations"]
    assert (got["schema"]["nodes"], got["schema"]["edges"]) == (4, 2)
    failed = h.call("lost")
    assert (failed.status, failed.data["code"]) == ("failed", "E_GRAPH_NODE")
    assert h.call("clear").data == {"nodes": 1, "edges": 1}
    assert ("account", "acme") not in fake.nodes and fake.edges == {}


def test_a_harness_without_a_graph_refuses_as_rrotor_does(tmp_path):
    def start(ctx, inputs):
        return Result.done(ctx.graph.schema())

    refused = Harness(program(start=start), tmp_path).call("start")
    assert (refused.status, refused.data["code"]) == ("failed", "E_GRAPH_UNDECLARED")
    reader = Harness(
        program(start=lambda ctx, inputs: Result.done(ctx.graph.upsert([ACME]))), tmp_path, graph=FakeGraph("read")
    )
    assert reader.call("start").data["code"] == "E_GRAPH_READONLY"
