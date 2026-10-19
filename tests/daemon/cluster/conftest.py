"""N nodes in one event loop: each its own /data, licence key and audit store, a real listener on 127.0.0.1 with the
mutual TLS its cluster secret derives, and the timings made short."""

from __future__ import annotations

import asyncio
import base64
import os
import socket
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import rrotor
from rrotord.audit import Audit
from rrotord.cluster import discovery
from rrotord.cluster import node as cluster_node
from rrotord.cluster.node import Cluster
from rrotord.config import Config
from rrotord.licence import Licence
from tests.fakes import keys

SECRET = os.urandom(32)


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cluster_node, "BIND", "127.0.0.1")
    for name, value in (("RETRY", 0.05), ("PING", 0.05), ("GATE", 0.05), ("HEAD_EVERY", 0.05), ("REJOIN", 0.1)):
        monkeypatch.setattr(cluster_node, name, value)
    monkeypatch.setattr(cluster_node, "ASKED_WITHIN", 2.0)
    monkeypatch.setattr(discovery, "RESOLVE_EVERY", 0.0)


def node_config(
    root: Path, node_id: str, port: int, peers: tuple[str, ...], folder: str | None = None, **extra: Any
) -> Config:
    home = root / (folder or node_id)
    settings: dict[str, Any] = {
        "node_id": node_id,
        "link": False,
        "cluster": True,
        "cluster_peers": peers,
        "cluster_port": port,
        "cluster_advertise": f"127.0.0.1:{port}",
        "cluster_secrets": (SECRET,),
    }
    return Config(
        home / "data",
        "self",
        "http://127.0.0.1:1",
        0,
        0,
        None,
        Path(rrotor.__file__).parent,
        home / "run",
        **(settings | extra),
    )


def make_licence(data: Path, key: str | None, clock: Callable[[], datetime] | None = None) -> Licence:
    licence = Licence(data, etc=data.parent / "etc" / "license.key", clock=clock or (lambda: datetime.now(UTC)))
    licence.start([("GLYPHH_LICENSE_KEY", key)] if key else [])
    return licence


class Nodes:
    """Make nodes of one cluster (or of another, with another secret) and stop them all at the end."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.all: list[Cluster] = []
        self.ports: dict[str, int] = {}

    def port(self, node_id: str) -> int:
        if node_id not in self.ports:
            self.ports[node_id] = free_port()
        return self.ports[node_id]

    def peers(self, ids: list[str]) -> tuple[str, ...]:
        return tuple(f"127.0.0.1:{self.port(i)}" for i in ids)

    def make(
        self,
        node_id: str,
        peers: list[str],
        key: str | None = "pro",
        tier: str = "pro",
        max_nodes: int | None = 3,
        folder: str | None = None,
        clock: Callable[[], datetime] | None = None,
        **extra: Any,
    ) -> Cluster:
        port = self.port(folder or node_id)
        c = node_config(self.root, node_id, port, self.peers(peers), folder, **extra)
        if key == "pro":
            key = keys().key(tier=tier, max_nodes=max_nodes)
        licence = make_licence(c.data, key, clock)
        audit = Audit(c.data / "audit", node_id, tier=lambda: licence.limits().tier)
        cluster = Cluster(c, licence, audit, "0.1.0")
        self.all.append(cluster)
        return cluster

    def again(self, old: Cluster, **changes: Any) -> Cluster:
        """The same node restarted: its /data, its licence key, a new process."""
        c = replace(old.config, **changes)
        licence = make_licence(c.data, None, old.licence.clock)
        cluster = Cluster(c, licence, Audit(c.data / "audit", c.node_id, tier=lambda: licence.limits().tier), "0.1.0")
        self.all.append(cluster)
        return cluster

    async def stop(self) -> None:
        for c in self.all:
            await c.stop()


@pytest.fixture
async def nodes(tmp_path: Path) -> AsyncIterator[Nodes]:
    made = Nodes(tmp_path)
    yield made
    await made.stop()


async def until(check: Callable[[], bool], within: float = 10.0) -> None:
    for _ in range(int(within / 0.02)):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("not reached in time")
