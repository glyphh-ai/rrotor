"""ctx.graph in the worker: asked of the runner over the channel and answered by its id; a runner that goes away
fails the call; a start that names no graph leaves ctx.graph refusing."""

from __future__ import annotations

import socket
from typing import Any

import pytest

from tests.sdk.worker.test_loop import Runner, run, start_for, write

pytestmark = pytest.mark.usefixtures("restore_signals", "forget_program")

READS = """
from rrotor import Result

def start(ctx, inputs):
    return Result.done([h["key"] for h in ctx.graph.search("renewal", limit=3)])
"""


@pytest.fixture
def forget_program():
    import sys

    yield
    sys.modules.pop("program", None)


def test_a_programs_ctx_graph_is_asked_of_the_runner_and_answered(tmp_path):
    folder = write(tmp_path / "p", READS)

    def react(message: dict[str, Any], runner: Runner) -> None:
        if message["op"] == "graph":
            runner.send("graph_result", id=0, result={})  # another call's answer: not this one
            runner.send("graph_result", id=message["id"], result={"status": "done", "data": {"hits": [{"key": "d1"}]}})
        elif message["op"] == "result":
            runner.send("end")

    runner = run(start_for(folder, graph="read"), react)
    assert runner.results()[0]["data"] == ["d1"]
    asked = next(m for m in runner.got if m["op"] == "graph")
    assert asked == {"op": "graph", "id": 1, "graph": "search", "query": "renewal", "limit": 3, "expand": False}


def test_a_ctx_graph_whose_runner_goes_away_fails(tmp_path):
    folder = write(tmp_path / "p", READS)

    def react(message: dict[str, Any], runner: Runner) -> None:
        if message["op"] == "graph":
            runner.mine.shutdown(socket.SHUT_WR)  # the runner gone: the worker reads the end of its channel

    runner = run(start_for(folder, graph="read"), react)
    assert runner.results()[0]["data"]["code"] == "E_GRAPH" and "went away" in runner.results()[0]["data"]["reason"]


def test_a_start_that_names_no_graph_leaves_ctx_graph_refusing(tmp_path):
    runner = run(start_for(write(tmp_path / "p", READS)))
    assert runner.results()[0]["data"]["code"] == "E_GRAPH_UNDECLARED"
    assert "graph" not in runner.ops()
