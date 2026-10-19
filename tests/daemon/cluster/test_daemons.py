"""N daemons in one event loop, each its cluster and its API: /readyz, GET /v1/cluster, MCP's cluster_status and the
health's `cluster`; the node past max_nodes answers 503 to everything but /healthz; Community never listens; the
heartbeat names the cluster and the role."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from rrotord.api.app import make
from rrotord.daemon import Daemon
from tests.daemon.cluster.conftest import Nodes, node_config, until
from tests.fakes import LocalLauncher, keys, ready

KEY = "rk_" + "k" * 30
HOST = "rrotor.acme.com"


class Fleet:
    def __init__(self, nodes: Nodes) -> None:
        self.nodes, self.daemons, self.clients = nodes, list[Daemon](), list[TestClient[Any, Any]]()

    async def add(self, node_id: str, ids: list[str], licence: str | None, **extra: Any) -> tuple[Daemon, Any]:
        c = node_config(
            self.nodes.root,
            node_id,
            self.nodes.port(node_id),
            self.nodes.peers(ids),
            runtime_id=str(uuid.uuid4()),
            runtime_token="tok_" + "t" * 20,
            public_host=HOST,
            keys_sha256=(hashlib.sha256(KEY.encode()).hexdigest(),),
            licence_env=(("GLYPHH_LICENSE_KEY", licence),) if licence else (),
            **extra,
        )
        d = Daemon(c, ready(), launcher=LocalLauncher())
        self.daemons.append(d)
        client = TestClient(TestServer(make(d.services())))
        await client.start_server()
        self.clients.append(client)
        await d.cluster.start()
        return d, client

    async def close(self) -> None:
        for d in self.daemons:
            await d.cluster.stop()
        for client in self.clients:
            await client.close()


@pytest.fixture
async def fleet(nodes: Nodes) -> AsyncIterator[Fleet]:
    made = Fleet(nodes)
    yield made
    await made.close()


async def get(client: Any, path: str, auth: bool = True) -> tuple[int, Any]:
    headers = {"Host": HOST, **({"Authorization": f"Bearer {KEY}"} if auth else {})}
    res = await client.get(path, headers=headers)
    return res.status, await res.json()


async def test_three_daemons_two_admitted_the_third_refused_and_out_of_the_load_balancer(fleet: Fleet):
    ids = ["n0", "n1", "n2"]
    two = keys().key(max_nodes=2)
    _, head_api = await fleet.add("n0", ids, two, cluster_head="n0")
    follower, follower_api = await fleet.add("n1", ids, two, cluster_head="n0")
    await until(lambda: follower.cluster.status == "ready")
    third, third_api = await fleet.add("n2", ids, two, cluster_head="n0")
    await until(lambda: third.cluster.status == "refused")
    assert await get(head_api, "/readyz", auth=False) == (200, {"ready": True})
    assert await get(follower_api, "/readyz", auth=False) == (200, {"ready": True})
    assert await get(third_api, "/readyz", auth=False) == (
        503,
        {"ready": False, "reason": "refused", "code": "E_CLUSTER_FULL"},
    )
    assert (await get(third_api, "/healthz", auth=False))[0] == 200
    status, body = await get(third_api, "/v1/models")
    assert (status, body["error"]["code"]) == (503, "E_CLUSTER_FULL")
    status, body = await get(head_api, "/v1/cluster")
    assert status == 200 and (body["role"], body["head"], body["max_nodes"], body["tier"]) == ("head", "n0", 2, "pro")
    assert [m["node_id"] for m in body["members"]] == ["n0", "n1"] and body["refused"][0]["node_id"] == "n2"
    status, health = await get(follower_api, "/v1/health")
    assert health["cluster"] | {"cluster_id": None} == {
        "cluster_id": None,
        "node_id": "n1",
        "role": "follower",
        "status": "ready",
        "ready": True,
        "head": "n0",
        "term": 1,
        "members": 2,
        "reason": None,
    }
    res = await follower_api.post(
        "/mcp",
        headers={"Host": HOST, "Authorization": f"Bearer {KEY}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "cluster_status"}},
    )
    result = (await res.json())["result"]
    assert result["isError"] is False and result["structuredContent"]["role"] == "follower"
    assert follower.heartbeat is None  # link off: no heartbeat; its body is below


async def test_community_never_listens_and_the_daemon_serves_standalone(fleet: Fleet, caplog):
    d, api = await fleet.add("solo", ["solo"], None, cluster_head="solo")
    await until(lambda: d.cluster.gated is False)
    assert "cluster synchronisation disabled under the Community licence" in caplog.text
    assert d.cluster.listener is None
    assert await get(api, "/readyz", auth=False) == (200, {"ready": True})
    assert (await get(api, "/v1/cluster"))[1]["role"] == "standalone"
    assert (await get(api, "/v1/health"))[1]["cluster"] == {"role": "standalone"}


async def test_serving_starts_and_stops_the_cluster_and_the_heartbeat_names_it(nodes: Nodes):
    c = node_config(
        nodes.root,
        "h0",
        nodes.port("h0"),
        nodes.peers(["h0"]),
        runtime_id=str(uuid.uuid4()),
        runtime_token="tok_" + "t" * 20,
        public_host=HOST,
        link=True,
        cluster_head="h0",
        licence_env=(("GLYPHH_LICENSE_KEY", keys().key()),),
    )
    d = Daemon(c, ready(), launcher=LocalLauncher())
    sent: list[dict[str, Any]] = []

    async def post(url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        sent.append(body)
        return 500, None

    assert d.heartbeat is not None
    d.heartbeat.post = post
    d.link.enabled = False
    stop = asyncio.Event()
    serving = asyncio.create_task(d.serve(stop))
    await until(lambda: d.cluster.role == "head")
    body = d.heartbeat.body(c.runtime_id or "")
    assert (body["cluster_id"], body["role"]) == (d.cluster.cluster_id, "head") and body["cluster_id"]
    d.licence.offer(keys().key(issued_at="2026-10-03T00:00:00Z"), "heartbeat")  # the gate looked at again
    assert d.cluster.changed.is_set() or d.cluster.gated
    stop.set()
    await asyncio.wait_for(serving, 10)
    assert d.cluster.listener is None and d.cluster.supervisor is None
    assert Path(c.data / "cluster" / "state.json").exists()
