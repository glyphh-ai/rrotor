"""Replication with a fixed head (docs/specs/cluster.md, 3), daemons of one cluster in one event loop: a new node
catches up from a snapshot, a model with data runs on the head alone and its data folder never syncs, writes are
forwarded and reads stay local, a follower never writes (a mislabelled function is refused), runs are reached on the
node that holds them, and deploys, keys and the licence ship from the head."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from rrotord.cluster import log as log_module
from tests.daemon.cluster.conftest import Nodes, until
from tests.daemon.cluster.ring import Ring, held, models, notes
from tests.fakes import GitRepo, keys, manifest


@pytest.fixture
async def ring(nodes: Nodes) -> AsyncIterator[Ring]:
    made = Ring(nodes)
    yield made
    await made.close()


def ready(ring: Ring, *ids: str) -> bool:
    return all(ring.daemons[i].cluster.ready()[0] for i in ids)


async def test_writes_run_on_the_head_reads_stay_local_and_a_new_node_catches_up_from_a_snapshot(ring: Ring):
    ids = ["n0", "n1", "n2"]
    head = await ring.add("n0", ids)
    await until(lambda: head.cluster.role == "head")
    models(head)
    status, body, _ = await ring.write("n0")
    assert (status, body["status"], body["data"]) == (200, "done", {"count": 1}), body
    one = await ring.add("n1", ids)
    await until(lambda: one.cluster.role == "follower" and ready(ring, "n1"))
    assert one.runner.models.active("notes") is not None and held(one) == 1  # the snapshot: the versions, the keys
    assert notes(one) == 0  # a data folder never syncs
    # a write on a follower goes to the head and runs there
    status, body, headers = await ring.write("n1")
    assert (status, body["status"]) == (200, "done") and body["run_id"].startswith("n0~"), body
    assert int(headers["Rrotor-Cluster-Seq"]) >= 1 and notes(head) == 2 and notes(one) == 0
    await until(lambda: held(one) == 2)
    # every call of a model with data runs on the head, even one marked as a read
    status, body, _ = await ring.call("n1", "count")
    assert (body["status"], body["data"]) == ("done", {"count": 2}) and body["run_id"].startswith("n0~"), body
    # a read of a model without data runs here, its folders read-only
    status, body, _ = await ring.call("n1", "query", {"q": 9}, model="lookup")
    assert body["status"] == "done" and body["run_id"].startswith("n1~"), body
    assert body["data"] == {"echo": {"q": 9}, "readonly": True}
    spec = ring.daemons["n1"].launcher.specs[-1]  # type: ignore[attr-defined]
    assert spec.mounts.readonly and spec.mounts.data is None
    two = await ring.add("n2", ids)
    await until(lambda: ready(ring, "n2"))
    assert held(two) == 2
    _, view = await ring.get("n0", "/v1/cluster")
    assert view["commit_seq"] == view["applied_seq"] >= 2
    await until(lambda: all(m["lag"] == 0 for m in ring.daemons["n0"].cluster.view()["members"]))
    _, view = await ring.get("n2", "/v1/cluster")
    assert view["status"] == "ready" and view["applied_seq"] == view["commit_seq"]


async def test_a_mislabelled_function_is_refused_on_a_follower_and_its_folders_are_untouched(ring: Ring):
    ids = ["n0", "n1"]
    head = await ring.add("n0", ids)
    await until(lambda: head.cluster.role == "head")
    models(head)
    one = await ring.add("n1", ids)
    await until(lambda: ready(ring, "n1"))
    status, body, _ = await ring.call("n1", "sneak", model="lookup")
    assert status == 200 and body["status"] == "failed", body
    assert body["data"]["code"] == "E_READ_ONLY" and "sneak" in body["data"]["reason"]
    assert not (one.runner.models.files("lookup") / "sneaked").exists()
    status, body, _ = await ring.call("n0", "sneak", model="lookup")  # on the head it is not read-only
    assert body["status"] == "done" and (head.runner.models.files("lookup") / "sneaked").is_file()


async def test_runs_are_reached_on_the_node_that_holds_them(ring: Ring):
    ids = ["n0", "n1", "n2"]
    head = await ring.add("n0", ids)
    await until(lambda: head.cluster.role == "head")
    models(head)
    await ring.add("n1", ids)
    await ring.add("n2", ids)
    await until(lambda: ready(ring, "n1", "n2"))
    # a write asked on n1 runs on the head, and waits there; n2 answers it
    _, waiting, _ = await ring.call("n1", "decide")
    assert waiting["status"] == "needs_input" and waiting["run_id"].startswith("n0~"), waiting
    run_id = waiting["run_id"]
    status, seen = await ring.get("n2", f"/v1/runs/{run_id}")
    assert (status, seen["status"], seen["run_id"]) == (200, "needs_input", run_id)
    status, done = await ring.post("n2", f"/v1/runs/{run_id}/continue", {"answer": "page oncall"})
    assert (status, done["status"]) == (200, "done") and done["data"]["outcome"] == "page oncall", done
    assert notes(head) == 1
    status, lines = await ring.get("n1", f"/v1/runs/{run_id}/log")
    assert status == 200 and any(line.get("event") == "continued" for line in lines["lines"])
    # a read on n1 is held by n1: n2 reaches it through the head, and gathers its approval
    _, asked, _ = await ring.call("n1", "ask", model="lookup")
    assert asked["status"] == "needs_approval" and asked["run_id"].startswith("n1~"), asked
    status, approvals = await ring.get("n2", "/v1/approvals")
    assert [a["run_id"] for a in approvals["approvals"]] == [asked["run_id"]]
    status, ended = await ring.post("n2", f"/v1/runs/{asked['run_id']}/continue", {"answer": {"approved": True}})
    assert (status, ended["status"], ended["data"]) == (200, "done", {"approved": True}), ended
    status, cancelled = await ring.post("n0", f"/v1/runs/{asked['run_id']}/cancel", {})
    assert (status, cancelled["error"]["code"]) == (409, "E_RUN_STATE")
    # a node that is not in the cluster holds nothing reachable
    status, lost = await ring.get("n1", "/v1/runs/gone~00000000-0000-4000-8000-000000000000")
    assert (status, lost["error"]["code"]) == (410, "E_RUN_LOST")
    # MCP's tools take the same roads
    res = await ring.clients["n2"].post(
        "/mcp",
        headers=ring.headers(),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "run_status", "arguments": {"run_id": run_id}},
        },
    )
    assert (await res.json())["result"]["structuredContent"]["status"] == "done"


async def test_deploys_keys_and_the_licence_are_shipped_from_the_head(ring: Ring, repo: GitRepo, tmp_path: Path):
    ids = ["n0", "n1"]
    head = await ring.add("n0", ids)
    await until(lambda: head.cluster.role == "head")
    one = await ring.add("n1", ids)
    await until(lambda: ready(ring, "n1"))
    sha = repo.model(manifest("echo", functions={"start": {"writes": False}}))
    bundle = base64.b64encode(repo.bundle(tmp_path / "b.bundle").read_bytes()).decode()
    status, deployed = await ring.post("n1", "/v1/models/deploy", {"bundle": bundle, "commit": sha})
    assert (status, deployed["model"], deployed["commit_sha"]) == (201, "echo", sha), deployed
    found = one.runner.models.active("echo")
    assert found is not None and found.commit_sha == sha and (found.src / "model.py").is_file()
    assert (found.venv / "bin").is_dir()  # the head's build, fetched: no git and no PyPI on the follower
    status, body = await ring.post("n1", "/v1/runs", {"model": "echo", "function": "start", "inputs": {"a": 1}})
    assert (status, body["data"], body["run_id"][:3]) == (200, {"echo": {"a": 1}}, "n1~"), body
    head.runner.models.remove(head.runner.models.active("echo"))  # type: ignore[arg-type]
    await until(lambda: one.runner.models.by_sha("echo", sha) is None)
    # the runtime keys and the licence key
    head.access.set_keys(["f" * 64])
    await until(lambda: one.access.keys == ["f" * 64])
    newer = keys().key(issued_at="2026-10-05T00:00:00Z", tier="team")
    head.licence.offer(newer, "heartbeat")
    await until(lambda: one.licence.held is not None and one.licence.held.raw == newer)
    assert one.licence.plan_name == "Team" and one.licence.held_from[0] == "cluster"  # type: ignore[index]
    head.licence.offer(newer, "heartbeat")  # the same key again ships nothing
    assert [e["kind"] for e in head.cluster.replication.log.read(0, 1000)].count("licence") == 1


async def test_a_node_of_another_architecture_refuses_the_head_and_is_not_ready(ring: Ring):
    ids = ["n0", "n1"]
    head = await ring.add("n0", ids)
    await until(lambda: head.cluster.role == "head")
    models(head)
    one = ring.daemon("n1", ids)
    one.cluster.replication.arch = "riscv64"
    await ring.start(one)
    await until(lambda: one.cluster.replication.failure is not None)
    assert one.cluster.ready() == (False, "catching_up", "E_CLUSTER_ARCH")
    _, body = await ring.get("n1", "/v1/cluster")
    assert body["status"] == "catching_up"


async def test_a_lagging_follower_catches_up_from_a_snapshot_past_the_log_kept(ring: Ring, monkeypatch):
    monkeypatch.setattr(log_module, "SEGMENT", 256)
    ids = ["n0", "n1"]
    head = await ring.add("n0", ids, cluster_log_keep=1024)
    await until(lambda: head.cluster.role == "head")
    one = await ring.add("n1", ids, cluster_log_keep=1024)
    await until(lambda: ready(ring, "n1"))
    await ring.stop("n1")
    models(head)
    for i in range(20):
        head.access.set_keys([f"{i:064x}"])
    assert head.cluster.replication.log.first > one.cluster.replication.applied + 1  # what it missed is gone
    again = await ring.add("n1", ids, cluster_log_keep=1024)
    await until(lambda: ready(ring, "n1") and again.runner.models.active("notes") is not None)
    assert (
        again.access.keys == [f"{19:064x}"] and again.cluster.replication.applied == head.cluster.replication.log.last
    )
    # and one that only missed what the log still holds catches up from the log
    await ring.stop("n1")
    head.access.set_keys(["a" * 64])
    log_before = again.cluster.replication.log.last
    back = await ring.add("n1", ids, cluster_log_keep=1024)
    await until(lambda: back.access.keys == ["a" * 64])
    assert back.cluster.replication.log.last == log_before + 1


async def test_reads_go_on_without_the_head_and_writes_wait_for_it(ring: Ring):
    ids = ["n0", "n1"]
    head = await ring.add("n0", ids)
    await until(lambda: head.cluster.role == "head")
    models(head)
    await ring.add("n1", ids)
    await until(lambda: ready(ring, "n1"))
    status, body, headers = await ring.call("n1", "query", {"q": 1}, model="lookup")
    seq = int(headers["Rrotor-Cluster-Seq"])
    # a caller that saw a later seq: n1 waits for it, then asks the head
    res = await ring.clients["n1"].post(
        "/v1/runs",
        json={"model": "lookup", "function": "query", "inputs": {"data": 1}},
        headers=ring.headers(**{"Rrotor-Min-Seq": str(seq + 50)}),
    )
    assert (await res.json())["run_id"].startswith("n0~")
    await ring.stop("n0")
    await until(lambda: ring.daemons["n1"].cluster.head_channel is None)
    status, body, _ = await ring.call("n1", "query", {"q": 1}, model="lookup")
    assert status == 200 and body["data"]["echo"] == {"q": 1}
    status, body, _ = await ring.write("n1")
    assert (status, body["error"]["code"]) == (503, "E_NO_HEAD")
    status, approvals = await ring.get("n1", "/v1/approvals")
    assert (status, approvals) == (200, {"approvals": []})  # its own, when the head does not answer
