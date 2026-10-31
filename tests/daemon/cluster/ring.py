"""Daemons of one cluster in one event loop, for replication's tests: each its own /data, its API on a test server,
its cluster listener on 127.0.0.1 with real mutual TLS, runs in the test launcher (the host's Python, no sandbox).

Two models: `notes`, with data (its data folder is the head's alone, so every call of it runs there), and `lookup`,
without (its reads run on the node asked). A write that replicates is `Ring.write`: `notes.record` asked on any node
(it runs on the head), then one runtime key more on the head, which every member applies from the log."""

from __future__ import annotations

import hashlib
import sys
import uuid
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from rrotord.api.app import make
from rrotord.daemon import Daemon
from rrotord.deploy import Tools
from tests.daemon.cluster.conftest import Nodes, node_config
from tests.fakes import LocalLauncher, install, keys, ready

KEY = "rk_" + "r" * 30
KEY_SHA = hashlib.sha256(KEY.encode()).hexdigest()
HOST = "rrotor.acme.com"

NOTES = {
    "name": "notes",
    "data": True,
    "functions": {"record": {}, "count": {}, "decide": {}},
}
NOTES_MODEL = '''
import json

from rrotor import Result


def _notes(ctx):
    return ctx.data / "notes.jsonl"


def _count(ctx):
    path = _notes(ctx)
    return len(path.read_text().splitlines()) if path.is_file() else 0


def _add(ctx, note):
    with _notes(ctx).open("a") as f:
        f.write(json.dumps(note) + "\\n")


def record(ctx, inputs):
    _add(ctx, inputs.get("data"))
    return Result.done({"count": _count(ctx)})


def count(ctx, inputs):
    return Result.done({"count": _count(ctx)})


def decide(ctx, inputs):
    """Waits for the person's answer, then records it: its run is held by the head, which ran it."""
    said = ctx.needs_input("What should happen?", key="outcome")
    _add(ctx, said)
    return Result.done({"outcome": said})
'''

LOOKUP = {
    "name": "lookup",
    "functions": {
        "query": {"writes": False},
        "sneak": {"writes": False},
        "ask": {"writes": False, "approvals": ["ok"]},
    },
}
LOOKUP_MODEL = '''
import errno

from rrotor import Result


def query(ctx, inputs):
    return Result.done({"echo": inputs.get("data"), "readonly": ctx.readonly})


def sneak(ctx, inputs):
    """Marked "writes": false, and writes anyway. On a follower its folders are mounted read-only, which the test
    launcher (no sandbox) cannot do: the refusal is the one the mount gives."""
    if ctx.readonly:
        raise OSError(errno.EROFS, "Read-only file system", str(ctx.files / "sneaked"))
    (ctx.files / "sneaked").write_text("x")
    return Result.done({"sneaked": True})


def ask(ctx, inputs):
    """A read that waits for an approval: its run is held by the node that ran it."""
    return Result.done({"approved": ctx.ask_approval("ok", "May it?")})
'''


def models(d: Daemon, sha: str = "a" * 40, version: int = 1) -> None:
    """Both models, as a deploy leaves them, on the head: their versions ship to every follower."""
    install(d.runner.models, NOTES, NOTES_MODEL, sha=sha, version=version)
    install(d.runner.models, LOOKUP, LOOKUP_MODEL, sha=sha, version=version)


def notes(d: Daemon) -> int:
    """The notes this node's data folder holds (only the head's has any)."""
    path = d.runner.models.data("notes") / "notes.jsonl"
    return len(path.read_text().splitlines()) if path.is_file() else 0


def held(d: Daemon) -> int:
    """The writes that replicated which this node has applied: the runtime keys past the ring's own."""
    return len([k for k in d.access.keys if k != KEY_SHA])


class Ring:
    def __init__(self, nodes: Nodes) -> None:
        self.nodes = nodes
        self.daemons: dict[str, Daemon] = {}
        self.clients: dict[str, TestClient[Any, Any]] = {}
        self.runtime_id = str(uuid.uuid4())

    def daemon(self, node_id: str, ids: list[str], licence: str | None = None, **extra: Any) -> Daemon:
        c = node_config(
            self.nodes.root,
            node_id,
            self.nodes.port(node_id),
            self.nodes.peers(ids),
            runtime_id=self.runtime_id,
            runtime_token="tok_" + "t" * 20,
            public_host=HOST,
            keys_sha256=(KEY_SHA,),
            licence_env=(("GLYPHH_LICENSE_KEY", licence or keys().key()),),
            cluster_head=ids[0],
            **extra,
        )
        d = Daemon(c, ready(), launcher=LocalLauncher())
        d.deployer.tools = Tools(python=sys.executable)
        return d

    async def start(self, d: Daemon) -> TestClient[Any, Any]:
        client = TestClient(TestServer(make(d.services())))
        await client.start_server()
        await d.cluster.start()
        self.daemons[d.config.node_id], self.clients[d.config.node_id] = d, client
        return client

    async def add(self, node_id: str, ids: list[str], licence: str | None = None, **extra: Any) -> Daemon:
        d = self.daemon(node_id, ids, licence, **extra)
        await self.start(d)
        return d

    async def stop(self, node_id: str) -> None:
        d, client = self.daemons.pop(node_id), self.clients.pop(node_id)
        await d.cluster.stop()
        await d.runner.shutdown()
        await client.close()

    async def close(self) -> None:
        for node_id in list(self.daemons):
            await self.stop(node_id)

    async def call(
        self, node_id: str, function: str, data: Any = None, model: str = "notes", **body: Any
    ) -> tuple[int, Any, Any]:
        inputs = {} if data is None else {"data": data}
        sent = {"model": model, "function": function, "inputs": inputs, **body}
        res = await self.clients[node_id].post("/v1/runs", json=sent, headers=self.headers())
        return res.status, await res.json(), res.headers

    async def write(self, node_id: str, note: Any = "note") -> tuple[int, Any, Any]:
        """A write asked on a node: `notes.record`, which runs on the head; once done there, one runtime key more on
        the node that ran it (the head), a `keys` entry every member applies."""
        status, body, headers = await self.call(node_id, "record", note)
        if status == 200 and body.get("status") == "done":
            head = self.daemons[str(body["run_id"]).split("~")[0]]
            more = hashlib.sha256(f"write {held(head) + 1}".encode()).hexdigest()
            head.access.set_keys([*head.access.keys, more])
        return status, body, headers

    def headers(self, **more: str) -> dict[str, str]:
        return {"Host": HOST, "Authorization": f"Bearer {KEY}", **more}

    async def get(self, node_id: str, path: str) -> tuple[int, Any]:
        res = await self.clients[node_id].get(path, headers=self.headers())
        return res.status, await res.json()

    async def post(self, node_id: str, path: str, body: Any) -> tuple[int, Any]:
        res = await self.clients[node_id].post(path, json=body, headers=self.headers())
        return res.status, await res.json()
