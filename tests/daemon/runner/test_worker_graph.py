"""A program's ctx.graph on the runner's side of the channel: served, answered by its id as `graph_result`; a worker
with no graph to serve refuses; one whose graph breaks is still answered."""

from __future__ import annotations

import json
from typing import Any

from rrotord.runner.launch import Spec
from rrotord.runner.worker import Worker
from tests.daemon.runner.test_worker import Script

GRAPH_ASK = (
    "import json\n"
    "for n in (1, 2):\n"
    "    os.write(fd, json.dumps({'op': 'graph', 'id': n, 'graph': 'schema'}).encode() + b'\\n')\n"
    "got = b''\n"
    "while got.count(b'\\n') < 2:\n"
    "    got += os.read(fd, 4096)\n"
    "os.write(fd, json.dumps({'op': 'result', 'got': got.decode()}).encode() + b'\\n')"
)


async def refuses(_: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError("no CLI is asked for here")


async def test_a_programs_ctx_graph_is_served_and_answered_by_its_id():
    async def graph(ask: dict[str, Any]) -> dict[str, Any]:
        if ask["id"] == 2:
            raise RuntimeError("broke")
        return {"status": "done", "data": {"asked": ask["graph"]}}

    w = await Worker.start(Script(GRAPH_ASK), lambda fd: Spec(None, None, fd), lambda m: None, refuses, graph)  # type: ignore[arg-type]
    message = await w.next(5)
    assert message is not None
    answers = sorted((json.loads(line) for line in message["got"].splitlines()), key=lambda a: a["id"])
    assert answers[0] == {"op": "graph_result", "id": 1, "result": {"status": "done", "data": {"asked": "schema"}}}
    assert answers[1]["result"]["data"] == {"code": "E_GRAPH", "reason": "broke"}
    await w.stop()


async def test_a_worker_with_no_graph_to_serve_refuses():
    w = await Worker.start(Script(GRAPH_ASK), lambda fd: Spec(None, None, fd), lambda m: None)  # type: ignore[arg-type]
    message = await w.next(5)
    assert message is not None and "This run has no graph." in message["got"]
    await w.stop()
