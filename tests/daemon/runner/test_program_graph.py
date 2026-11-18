"""A pipeline and a reader, end to end (ctx.graph): real worker processes (the host's Python, no sandbox), a program
that writes the rrotor's graph and one that reads it, the runner checking what each manifest allows, the write marked
with its program and noted in the run's log; refusals answered as the program's own failure."""

from __future__ import annotations

from typing import Any

import pytest

from rrotord.graph import Graph
from tests.daemon.runner.test_engine import Env
from tests.fakes import install, manifest, outcome

PIPELINE = """
from rrotor import Result

DEALS = [
    {"id": "d1", "name": "Renewal 2027", "notes": "The renewal closed in March.", "stage": "won", "account": "acme"},
    {"id": "d2", "name": "Expansion", "notes": "Twenty more seats.", "stage": "open", "account": "acme"},
]

def start(ctx, inputs):
    ctx.graph.upsert(nodes=[{"type": "account", "key": "acme", "title": "Acme Corp", "props": {"tier": "gold"}}])
    wrote = ctx.graph.rows(DEALS, type="deal", key="id", title="name", text=["notes"],
                           links={"account": ("belongs_to", "account")})
    return Result.done(wrote)

def prune(ctx, inputs):
    return Result.done(ctx.graph.delete(nodes=[{"type": "deal", "key": "d2"}]))
"""

READER = """
from rrotor import GraphError, Result

def start(ctx, inputs):
    hits = ctx.graph.search(inputs["q"], expand=True)
    return Result.done({
        "hits": [(h["key"], h["passage"], h["out"][0]["key"] if h["out"] else None) for h in hits],
        "won": [n["key"] for n in ctx.graph.nodes("deal", where={"stage": "won"})],
        "around": sorted(n["key"] for n in ctx.graph.neighbours("account", "acme")["nodes"]),
        "node": ctx.graph.node("account", "acme")["source"],
        "none": ctx.graph.node("account", "nope"),
        "types": [t["type"] for t in ctx.graph.schema()["types"]],
    })

def write(ctx, inputs):
    try:
        ctx.graph.upsert(nodes=[{"type": "deal", "key": "x"}])
    except GraphError as err:
        return Result.done(err.code)

def bad(ctx, inputs):
    return Result.done(ctx.graph.search(""))
"""


@pytest.fixture
async def env(tmp_path, short_dir):
    e = Env(tmp_path / "data", short_dir)
    e.runner.graph = Graph(tmp_path / "data")
    yield e
    await e.runner.shutdown()


def deploy(env: Env) -> None:
    writes: dict[str, Any] = {"start": {}, "prune": {}}
    install(env.programs, manifest("crm_sync", graph="write", functions=writes), PIPELINE, sha="c" * 40)
    reads: dict[str, Any] = {"start": {}, "write": {}, "bad": {}}
    install(env.programs, manifest("reader", graph="read", functions=reads), READER, sha="d" * 40)


async def test_a_pipeline_writes_the_graph_and_a_reader_searches_it(env):
    deploy(env)
    wrote = await env.runner.call(program="crm_sync", function="start", inputs={})
    assert (wrote.status, outcome(wrote)["data"]) == ("done", {"nodes": 2, "edges": 2}), wrote.result
    noted = [line for line in env.records.lines(wrote.id) if line.get("event") == "graph"]
    assert noted[0] == {**noted[0], "op": "upsert", "nodes": 1, "edges": 0} and len(noted) == 3
    read = await env.runner.call(program="reader", function="start", inputs={"q": "renewal"})
    data = outcome(read)["data"]
    assert read.status == "done", read.result
    assert data["hits"] == [["d1", "Renewal 2027\n\nstage: won\n\nThe renewal closed in March.", "acme"]]
    assert (data["won"], data["around"]) == (["d1"], ["acme", "d1", "d2"])
    assert (data["node"], data["none"], data["types"]) == ("crm_sync", None, ["account", "deal"])
    assert not [line for line in env.records.lines(read.id) if line.get("event") == "graph"]  # reads are not noted
    pruned = await env.runner.call(program="crm_sync", function="prune", inputs={})
    assert outcome(pruned)["data"] == {"nodes": 1, "edges": 0}
    assert env.runner.graph is not None and env.runner.graph.schema()["nodes"] == 2


async def test_what_a_manifest_does_not_allow_is_refused_in_the_program_and_by_the_runner(env):
    deploy(env)
    refused = await env.runner.call(program="reader", function="write", inputs={})
    assert outcome(refused)["data"] == "E_GRAPH_READONLY"  # ctx.graph itself refuses, before asking
    run = await env.runner.call(program="reader", function="bad", inputs={})
    assert (run.status, run.result["data"]["code"]) == ("failed", "E_GRAPH")
    assert "query is what to look for" in run.result["data"]["reason"]
    # a worker that asks anyway (a program that went around ctx.graph) is refused by the runner
    answer = await env.runner.program_graph(run, {"graph": "upsert", "nodes": [{"type": "deal", "key": "x"}]})
    assert (answer["status"], answer["data"]["code"], answer["step"]) == ("failed", "E_GRAPH_READONLY", "graph")
    install(env.programs, manifest("plain"), READER.replace("def bad", "def other"), sha="e" * 40)
    plain = await env.runner.call(program="plain", function="start", inputs={"q": "x"})
    assert (plain.status, plain.result["data"]["code"]) == ("failed", "E_GRAPH_UNDECLARED")
    answer = await env.runner.program_graph(plain, {"graph": "schema"})
    assert answer["data"]["code"] == "E_GRAPH_UNDECLARED"
    assert env.runner.graph is not None and env.runner.graph.schema()["nodes"] == 0


async def test_a_rrotor_that_keeps_no_graph_fails_the_call(env):
    deploy(env)
    graph = env.runner.graph
    assert graph is not None
    env.runner.graph = None
    run = await env.runner.call(program="crm_sync", function="start", inputs={})
    assert (run.status, run.result["data"]["code"]) == ("failed", "E_GRAPH")
    assert "keeps no graph" in run.result["data"]["reason"]
    graph.close()
