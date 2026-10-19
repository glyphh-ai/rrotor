"""Daemons of one cluster in one event loop, for replication's tests: each its own /data, its API on a test server,
its cluster listener on 127.0.0.1 with real mutual TLS, the Ada part (the kernel in tests/kernel) installed, runs in
the test launcher (the host's Python, no sandbox) on a real SQLite store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from rrotor import ada
from rrotord.api.app import make
from rrotord.daemon import Daemon
from rrotord.deploy import Tools
from rrotord.parts import Parts
from tests.daemon.cluster.conftest import Nodes, node_config
from tests.fakes import LocalLauncher, ada_part, install, keys, ready

KEY = "rk_" + "r" * 30
HOST = "rrotor.acme.com"
TICKETS = Path(__file__).parent.parent.parent.parent / "examples" / "ada_tickets"
_PART: dict[str, tuple[bytes, str, str]] = {}


def part(root: Path) -> tuple[bytes, str, str]:
    if "ada" not in _PART:
        _PART["ada"] = ada_part(root)
    return _PART["ada"]


def ticket(i: int, title: str = "login fails", area: str = "auth", severity: int = 8) -> dict[str, Any]:
    return {
        "issue": {"body": {"title": title, "component": area, "severity": severity, "id": i}},
        "meta": {"time": {"opened": "2026-10-01T12:00:00Z"}, "owner": {"team": "infra"}},
    }


SNEAK = '''

def sneak(ctx, inputs):
    """Marked "writes": false, and records anyway."""
    ctx.ada.record(inputs["data"], "sneaked")
    return Result.done({"sneaked": True})


def ask(ctx, inputs):
    """A read that waits for an approval: its run is held by the node that ran it."""
    return Result.done({"approved": ctx.ask_approval("ok", "May it?")})
'''


def tickets(d: Daemon, sha: str = "a" * 40, version: int = 1) -> None:
    """The tickets example, as a deploy leaves it, with one function more that is mislabelled."""
    data = json.loads((TICKETS / "glyphh.json").read_text())
    data["functions"]["sneak"] = {"writes": False, "inputs": {"type": "object"}}
    data["functions"]["ask"] = {"writes": False, "approvals": ["ok"]}
    made = install(d.runner.models, data, (TICKETS / "model.py").read_text() + SNEAK, sha=sha, version=version)
    (made.src / "spec.json").write_text((TICKETS / "spec.json").read_text())


def rows(d: Daemon, model: str = "tickets") -> int:
    store = d.runner.models.data(model) / ada.STORE
    if not store.is_file():
        return 0
    return ada.count(store)


def meta(d: Daemon, key: str, model: str = "tickets") -> Any:
    store = d.runner.models.data(model) / ada.STORE
    db = sqlite3.connect(f"{store.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    finally:
        db.close()
    return json.loads(row[0]) if row else None


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
            keys_sha256=(hashlib.sha256(KEY.encode()).hexdigest(),),
            licence_env=(("GLYPHH_LICENSE_KEY", licence or keys().key()),),
            cluster_head=ids[0],
            **extra,
        )
        d = Daemon(c, ready(), launcher=LocalLauncher())
        d.parts = Parts(c.data, keys().public, "arm64")
        d.parts.install("ada", "3.10.0", *part(self.nodes.root))
        d.runner.parts = d.parts
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
        self, node_id: str, function: str, data: Any = None, outcome: str | None = None, **body: Any
    ) -> tuple[int, Any, Any]:
        inputs = ({} if data is None else {"data": data}) | ({} if outcome is None else {"outcome": outcome})
        sent = {"model": "tickets", "function": function, "inputs": inputs, **body}
        res = await self.clients[node_id].post("/v1/runs", json=sent, headers=self.headers())
        return res.status, await res.json(), res.headers

    def headers(self, **more: str) -> dict[str, str]:
        return {"Host": HOST, "Authorization": f"Bearer {KEY}", **more}

    async def get(self, node_id: str, path: str) -> tuple[int, Any]:
        res = await self.clients[node_id].get(path, headers=self.headers())
        return res.status, await res.json()

    async def post(self, node_id: str, path: str, body: Any) -> tuple[int, Any]:
        res = await self.clients[node_id].post(path, json=body, headers=self.headers())
        return res.status, await res.json()
