"""Nodes find and authenticate each other, the lowest node id makes the cluster, the rest join it up to the licence
key's max_nodes; the node past it is refused, logged and audited, and asks again; leaving, evicting and a downgrade;
the key's gate; a restart. Real listeners on 127.0.0.1, real mutual TLS from the cluster secret."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from rrotord.cluster import node as cluster_node
from rrotord.cluster.node import Cluster
from rrotord.cluster.transport import ClusterError
from rrotord.errors import BadRequest
from rrotord.store import write_json
from tests.daemon.cluster.conftest import Nodes, free_port, until
from tests.fakes import keys


def audited(c: Cluster, kind: str) -> list[dict[str, Any]]:
    folder = c.config.data / "audit"
    lines = [json.loads(line) for f in sorted(folder.glob("*.jsonl")) for line in f.read_text().splitlines()]
    return [e for e in lines if e["kind"] == kind]


async def test_three_nodes_find_each_other_the_lowest_makes_the_cluster_and_the_rest_join(nodes: Nodes):
    ids = ["rrotor-0", "rrotor-1", "rrotor-2"]
    made = [nodes.make(i, ids, tier="enterprise") for i in reversed(ids)]
    for c in made:
        await c.start()
    await until(lambda: all(c.status == "ready" for c in made) and len(made[-1].members()) == 3)
    head, *followers = reversed(made)
    assert (head.node_id, head.role) == ("rrotor-0", "head")
    assert all(f.role == "follower" and f.state["head"] == "rrotor-0" for f in followers)
    assert len({c.cluster_id for c in made}) == 1 and head.cluster_id is not None
    assert audited(head, "cluster.head")[0]["term"] == 1
    assert sorted(e["node"] for e in audited(head, "cluster.join")) == ["rrotor-1", "rrotor-2"]
    assert all(e["cluster_id"] == head.cluster_id for e in audited(head, "cluster.join"))
    await until(lambda: len(followers[0].view()["members"]) == 3)
    view = followers[0].view()
    assert (view["role"], view["head"], view["term"], view["commit_seq"], view["applied_seq"]) == (
        "follower",
        "rrotor-0",
        1,
        0,
        0,
    )
    assert sorted(m["node_id"] for m in view["members"]) == ids and all(m["voter"] for m in view["members"])
    assert head.view()["members"][0]["role"] == "head" and head.view()["tier"] == "enterprise"
    assert head.ready() == (True, None, None) and followers[0].identity() == (head.cluster_id, "follower")


async def test_the_node_past_max_nodes_is_refused_logged_audited_and_asks_again(nodes: Nodes, caplog):
    caplog.set_level(logging.INFO)
    ids = ["a", "b", "c"]
    a = nodes.make(
        "a", ids, max_nodes=2, tier="enterprise", cluster_head="a"
    )  # it makes the cluster without waiting for b and c
    await a.start()
    await until(lambda: a.role == "head")
    b = nodes.make("b", ids, max_nodes=2, tier="enterprise")
    await b.start()
    await until(lambda: b.status == "ready")
    c = nodes.make("c", ids, max_nodes=2, tier="enterprise")
    await c.start()
    await until(lambda: c.status == "refused")
    assert c.ready() == (False, "refused", "E_CLUSTER_FULL") and c.role == "standalone"
    assert c.refusal is not None and "max_nodes" in c.refusal["message"]
    assert [r["node_id"] for r in a.view()["refused"]] == ["c"] and a.view()["refused"][0]["code"] == "E_CLUSTER_FULL"
    assert audited(a, "cluster.refused")[0]["code"] == "E_CLUSTER_FULL"
    assert any(r.levelname == "ERROR" and r.getMessage() == "cluster: this node is refused" for r in caplog.records)
    refusals = len(audited(a, "cluster.refused"))
    await until(lambda: len(audited(a, "cluster.refused")) > refusals)  # it asked again
    assert len(a.members()) == 2
    # b leaves: c is let in at its next ask
    done = await a._command({"kind": "command"}, {"name": "leave", "node": "b"})
    assert done == {"removed": "b", "members": 1}
    await until(lambda: c.status == "ready" and b.status == "refused")
    assert b.refusal is not None and b.refusal["code"] == "E_CLUSTER_LEFT"
    assert [m.node_id for m in a.members()] == ["a", "c"] and a.view()["refused"] == []
    assert audited(a, "cluster.leave")[0]["node"] == "b"


async def start(*clusters: Cluster) -> None:
    for c in clusters:
        await c.start()


async def test_the_node_named_by_rrotor_cluster_head_makes_the_cluster(nodes: Nodes):
    a, b = nodes.make("a", ["a", "b"], cluster_head="b"), nodes.make("b", ["a", "b"], cluster_head="b")
    await start(a, b)
    await until(lambda: a.status == "ready" and b.status == "ready")
    assert (a.role, b.role) == ("follower", "head")


async def test_with_a_peer_silent_and_no_head_named_nobody_makes_a_cluster(nodes: Nodes):
    a = nodes.make("a", ["a", "b"])
    await start(a)
    await until(lambda: a.status == "no_cluster")
    assert a.ready() == (False, "no_cluster", None) and a.cluster_id is None and a.identity() == (None, "standalone")
    assert a.view()["members"] == [] and a.short()["ready"] is False


async def test_a_node_with_a_foreign_secret_is_refused_at_the_handshake_and_stays_not_ready(nodes: Nodes, caplog):
    a = nodes.make("a", ["a", "z"], cluster_head="a")
    z = nodes.make("z", ["a", "z"], cluster_head="a", cluster_secrets=(b"x" * 32,))
    await start(a, z)
    await until(lambda: a.role == "head")
    await until(lambda: "cluster: peer certificate not from this cluster" in caplog.text)
    await asyncio.sleep(0.2)
    assert z.status == "no_cluster" and not z.ready()[0] and len(a.members()) == 1


async def test_a_second_node_with_a_live_members_id_is_refused(nodes: Nodes):
    ids = ["a", "b"]
    a, b = nodes.make("a", ids, cluster_head="a", tier="enterprise"), nodes.make("b", ids, cluster_head="a")
    await start(a, b)
    await until(lambda: b.status == "ready")
    twin = nodes.make("b", ["a"], cluster_head="a", folder="b-twin")
    await start(twin)
    await until(lambda: twin.status == "refused")
    assert twin.ready() == (False, "refused", "E_CLUSTER_NODE_ID") and b.status == "ready"
    assert audited(a, "cluster.refused")[0]["node"] == "b"
    head_twin = nodes.make("a", ["a"], cluster_head="z", folder="a-twin")  # the head's own id
    head_twin.port = free_port()
    await start(head_twin)
    await until(lambda: head_twin.status == "refused")
    assert head_twin.refusal is not None and head_twin.refusal["code"] == "E_CLUSTER_NODE_ID"


async def test_a_restarted_follower_and_a_restarted_head_find_each_other_again(nodes: Nodes, caplog):
    ids = ["a", "b"]
    a, b = nodes.make("a", ids, tier="enterprise"), nodes.make("b", ids)
    await start(a, b)
    await until(lambda: b.status == "ready")
    cluster_id = a.cluster_id
    await b.stop()
    b2 = nodes.again(b)
    assert b2.status == "joining" and b2.ready() == (False, "joining", None)
    await start(b2)
    await until(lambda: b2.status == "ready")
    assert len(a.members()) == 2 and len(audited(a, "cluster.join")) == 1  # let back in, not counted again
    await a.stop()
    await until(lambda: "cluster: lost the head; asking again" in caplog.text)
    assert b2.status == "ready"  # reads go on being served while the head is away
    a2 = nodes.again(a)
    await start(a2)
    await until(lambda: a2.role == "head" and len(a2.links) == 1)
    assert a2.cluster_id == cluster_id and b2.cluster_id == cluster_id


async def test_a_follower_restarted_while_its_head_is_down_waits_not_ready(nodes: Nodes):
    ids = ["a", "b"]
    a, b = nodes.make("a", ids), nodes.make("b", ids)
    await start(a, b)
    await until(lambda: b.status == "ready")
    await a.stop()
    await b.stop()
    b2 = nodes.again(b)
    await start(b2)
    await asyncio.sleep(0.3)
    assert b2.ready() == (False, "joining", None) and b2.cluster_id == a.cluster_id


async def test_a_member_whose_disk_was_replaced_takes_its_place_once_the_old_one_is_gone(nodes: Nodes):
    ids = ["a", "b"]
    a, b = nodes.make("a", ids), nodes.make("b", ids)
    await start(a, b)
    await until(lambda: b.status == "ready")
    await b.stop()
    await until(lambda: "b" not in a.links)
    fresh = nodes.make("b", ["a"], folder="b-new")
    await start(fresh)
    await until(lambda: fresh.status == "ready")
    assert [m.instance for m in a.members() if m.node_id == "b"] == [fresh.state["instance"]]


async def test_a_member_unseen_past_evict_after_is_removed_and_may_join_again(nodes: Nodes):
    ids = ["a", "b"]
    a = nodes.make("a", ids, tier="enterprise", cluster_evict_after=0.2)
    b = nodes.make("b", ids)
    await start(a, b)
    await until(lambda: b.status == "ready")
    await b.stop()
    await until(lambda: len(a.members()) == 1)
    assert audited(a, "cluster.leave")[0]["reason"] == "unseen"
    b2 = nodes.again(b)
    await start(b2)
    await until(lambda: b2.status == "ready" and len(a.members()) == 2)


async def test_a_key_with_a_smaller_max_nodes_removes_the_members_joined_last(nodes: Nodes):
    ids = ["a", "b", "c"]
    a = nodes.make("a", ids, tier="enterprise", max_nodes=3)
    b, c = nodes.make("b", ids), nodes.make("c", ids)
    await start(a, b, c)
    await until(lambda: len(a.links) == 2)
    last = a.members()[-1].node_id
    a.licence.offer(keys().key(tier="enterprise", max_nodes=2, issued_at="2026-10-02T00:00:00Z"), "heartbeat")
    a.licence_changed()
    by_id = {"b": b, "c": c}
    await until(lambda: by_id[last].status == "refused")
    refusal = by_id[last].refusal
    assert refusal is not None and refusal["code"] == "E_CLUSTER_FULL"
    assert [m.node_id for m in a.members()] == ["a", "b" if last == "c" else "c"]
    assert audited(a, "cluster.leave")[0] == audited(a, "cluster.leave")[0] | {"node": last, "reason": "max_nodes"}
    assert [r["node_id"] for r in a.view()["refused"]] == [last]


async def test_community_never_listens_and_says_why(nodes: Nodes, caplog):
    a = nodes.make("a", ["a"], key=None, cluster_head="a")
    await start(a)
    await until(lambda: a.gated is False)
    assert "cluster synchronisation disabled under the Community licence" in caplog.text
    assert a.listener is None and a.status == "standalone" and a.ready() == (True, None, None)
    with socket.socket() as s:
        assert s.connect_ex(("127.0.0.1", a.port)) != 0  # nothing listens on the cluster port
    assert a.view() == {
        "role": "standalone",
        "node_id": "a",
        "status": "standalone",
        "reason": "cluster synchronisation disabled under the Community licence",
    }
    assert a.short() == {"role": "standalone"} and a.identity() == (None, "standalone")
    b = nodes.make("b", ["b"], key=keys().key(cluster_sync_enabled=False), cluster_head="b")
    await start(b)
    await until(lambda: "cluster synchronisation disabled under this licence" in caplog.text)
    assert b.listener is None


async def test_the_cluster_off_is_standalone_and_never_starts(nodes: Nodes):
    a = nodes.make("a", ["a"], cluster=False)
    await a.start()
    assert a.supervisor is None and a.view() == {"role": "standalone"} and a.short() == {"role": "standalone"}
    assert a.ready() == (True, None, None)


async def test_past_grace_the_head_runs_alone_the_followers_are_refused_and_a_new_key_resumes(nodes: Nodes):
    now, fixed = [datetime.now(UTC)], datetime.now(UTC)
    ids = ["a", "b", "c"]
    expiring = keys().key(expires_at=(now[0] + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    a = nodes.make("a", ids, key=expiring, clock=lambda: now[0])
    b = nodes.make("b", ids, key=expiring, clock=lambda: now[0])
    c = nodes.make("c", ids, key=expiring, clock=lambda: fixed)  # its own key still counts: the head tells it
    await start(a, b, c)
    await until(lambda: b.status == "ready" and c.status == "ready")
    now[0] += timedelta(days=20)
    await until(lambda: a.status == "standalone" and b.listener is None and c.status == "refused")
    assert a.listener is None and b.listener is None and a.cluster_id is None and c.listener is not None
    for follower in (b, c):
        assert follower.refusal is not None and follower.refusal["code"] == "E_LICENCE_CLUSTER"
        assert follower.cluster_id is not None
    now[0] -= timedelta(days=20)
    await until(lambda: a.role == "head" and b.status == "ready" and c.status == "ready")


async def test_the_transport_that_cannot_start_is_tried_again(nodes: Nodes, caplog):
    a = nodes.make("a", ["a"], cluster_head="a")
    taken = socket.socket()
    taken.bind(("127.0.0.1", a.port))
    taken.listen()
    try:
        await start(a)
        await until(lambda: "cluster: the transport could not start" in caplog.text)
        assert a.listener is None or a.role != "head"
    finally:
        taken.close()
    await until(lambda: a.role == "head")


async def test_a_failed_step_is_logged_and_tried_again(nodes: Nodes, monkeypatch, caplog):
    a = nodes.make("a", ["a"], cluster_head="a")
    real, calls = a._member, []

    async def flaky() -> float:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk full")
        return await real()

    monkeypatch.setattr(a, "_member", flaky)
    await start(a)
    await until(lambda: a.role == "head")
    assert "cluster: finding the head failed" in caplog.text


async def test_found_by_dns_the_addresses_resolved_on_the_cluster_port(nodes: Nodes):
    a = nodes.make("a", ["a"], cluster_head="a")
    await start(a)
    await until(lambda: a.role == "head")

    async def resolve(name: str, port: int) -> list[Any]:
        assert name == "rrotor-headless.rrotor.svc.cluster.local"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    b = nodes.make(
        "b", [], cluster_peers=(), cluster_dns="rrotor-headless.rrotor.svc.cluster.local", cluster_port=a.port
    )
    b.discovery.resolve = resolve
    b.port = free_port()  # its own listener elsewhere: one host, so one port each
    await start(b)
    await until(lambda: b.status == "ready")


async def test_a_node_of_another_cluster_is_refused(nodes: Nodes):
    a = nodes.make("a", ["a", "b"], cluster_head="a")
    b = nodes.make("b", ["a", "b"], cluster_head="a")
    write_json(b.path, {"instance": str(uuid.uuid4()), "cluster_id": str(uuid.uuid4()), "head": "q", "term": 1})
    b.state = json.loads(b.path.read_text())
    await start(a, b)
    await until(lambda: b.status == "refused")
    assert b.refusal is not None and b.refusal["code"] == "E_CLUSTER_ID"


async def test_an_address_that_is_not_the_head_is_not_joined(nodes: Nodes):
    ids = ["a", "b", "c"]
    a, b = nodes.make("a", ids, cluster_head="a"), nodes.make("b", ids, cluster_head="a")
    await start(a, b)
    await until(lambda: b.status == "ready")
    c = nodes.make("c", ids, cluster_head="a")
    await c._open()
    assert c.membership is not None
    c.membership.cancel()  # step by hand
    assert await c._join(b.address) == cluster_node.RETRY and c.status == "joining"
    assert await c._join(f"127.0.0.1:{free_port()}") == cluster_node.RETRY
    await c._shut()


async def test_leaving_by_command_through_a_follower_and_joining_again_when_asked(nodes: Nodes):
    ids = ["a", "b", "c"]
    a, b, c = (nodes.make(i, ids, tier="enterprise") for i in ids)
    await start(a, b, c)
    await until(lambda: b.status == "ready" and c.status == "ready")
    done = await b._command({"kind": "command"}, {"name": "leave", "node": "c"})  # b passes it to the head
    assert done == {"removed": "c", "members": 2}
    await until(lambda: c.status == "refused")
    assert c.refusal is not None and c.refusal["code"] == "E_CLUSTER_LEFT"
    assert audited(a, "cluster.leave")[0]["actor"] == {"type": "local"}
    await asyncio.sleep(0.3)
    assert c.status == "refused"  # it does not ask again by itself
    answered = await c._command({"kind": "command"}, {"name": "join"})
    assert answered["status"] == "ready" and answered["role"] == "follower" and len(a.members()) == 3
    same = await a._command({"kind": "command"}, {"name": "join"})  # on the head: nothing to do
    assert same["role"] == "head"
    await b._command({"kind": "command"}, {"name": "leave", "node": "c"})
    await until(lambda: c.status == "refused")
    await c.stop()
    again = nodes.again(c)  # restarted, it asks once and is told it left
    await again.start()
    await until(lambda: again.status == "refused")
    assert again.refusal is not None and again.refusal["code"] == "E_CLUSTER_LEFT"


async def test_what_the_listener_refuses(nodes: Nodes):
    a = nodes.make("a", ["a"], cluster_head="a")
    await start(a)
    await until(lambda: a.role == "head")
    with pytest.raises(ClusterError) as err:
        await a._command({"kind": "command"}, {"name": "leave", "node": "a"})
    assert err.value.code == "E_CLUSTER_HEAD"
    with pytest.raises(ClusterError) as err:
        await a._command({"kind": "command"}, {"name": "leave", "node": "nobody"})
    assert (err.value.code, err.value.status) == ("E_CLUSTER_MEMBER", 404)
    for bad in ({"name": "leave", "node": "Not An Id"}, {"name": "leave"}, {"name": "dance"}):
        with pytest.raises(BadRequest):
            await a._command({"kind": "command"}, bad)
    with pytest.raises(BadRequest):
        await a._command({"kind": "node"}, {"name": "join"})  # only the command asks a node to join
    lone = nodes.make("z", ["z"], cluster_head="y")
    with pytest.raises(ClusterError) as err:
        await lone._command({"kind": "command"}, {"name": "leave", "node": "a"})
    assert err.value.code == "E_NO_HEAD"
    with pytest.raises(ClusterError) as err:
        lone._admit(None, {"node_id": "q"}, {})  # type: ignore[arg-type]
    assert err.value.code == "E_NOT_HEAD"
    with pytest.raises(ClusterError) as err:
        lone._ping({"member": "q"}, {})
    assert err.value.code == "E_CLUSTER_MEMBER"


def test_what_a_follower_is_pushed(nodes: Nodes):
    a = nodes.make("a", ["a"])
    a._pushed({"type": "something"}, {})
    a._pushed({"type": "append"}, {})  # no head connection yet: nothing to answer on
    assert a.refusal is None
    a._pushed({"type": "removed"}, {})
    assert a.refusal is not None and a.refusal["code"] == "E_CLUSTER_FULL" and a.status == "refused"
    assert cluster_node.Member.of({"node_id": "x"}) is None and cluster_node.names(None) == []


class Gone:
    """A head's connection that is gone: every request fails as closed."""

    def __init__(self) -> None:
        self.closed = 0

    async def request(self, kind: str, **_: Any) -> dict[str, Any]:
        raise ClusterError("The connection closed.", code="E_CLUSTER_CLOSED")

    async def close(self) -> None:
        self.closed += 1


async def test_a_follower_whose_head_goes_stops_following_and_says_so(nodes: Nodes, caplog):
    b = nodes.make("b", ["b"])
    gone = Gone()
    reader = asyncio.create_task(asyncio.sleep(10))
    await b._follow(gone, reader)  # type: ignore[arg-type]
    await asyncio.gather(reader, return_exceptions=True)
    assert reader.cancelled() and gone.closed == 1 and b.head_channel is None
    assert "cluster: lost the head; asking again" in caplog.text
    done = asyncio.create_task(asyncio.sleep(0))
    await done
    await b._follow(gone, done)  # type: ignore[arg-type]
    assert gone.closed == 2


def test_a_head_whose_key_stops_allowing_a_cluster_cuts_nobody_and_wakes_the_gate(nodes: Nodes):
    a = nodes.make("a", ["a"], key=None)
    a.state["members"] = [cluster_node.Member(n, "", "", n, "t").to_json() for n in ("a", "b")]
    a._fit()
    assert a.changed.is_set() and len(a.members()) == 2


async def test_stopping_cancels_what_the_node_was_still_telling(nodes: Nodes):
    c = nodes.make("solo", ["solo"], cluster_head="solo")
    c._spawn(asyncio.sleep(30))
    await c.stop()
    assert not c.telling
