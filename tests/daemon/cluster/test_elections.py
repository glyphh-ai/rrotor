"""Elected heads (docs/specs/cluster.md, 4 and 11, step 8), daemons of an Enterprise cluster in one event loop: the
head lost, another voter is elected and the writes resume on it; while there is none, writes are 503 `E_NO_HEAD` with
`Retry-After`; a head that cannot reach a majority of voters answers `E_NOT_COMMITTED`, then steps down, and what it
kept is committed when they return; an elected head restarted follows; promotion under HA moves the head only to an
up-to-date voter; voters are the first five members, the rest learners, a learner made a voter when one leaves."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from rrotord.cluster import command, replication
from rrotord.cluster import node as cluster_node
from rrotord.cluster.raft import Timing
from rrotord.errors import RrotorError
from tests.daemon.cluster.conftest import Nodes, until
from tests.daemon.cluster.ring import Ring, rows, ticket, tickets
from tests.fakes import keys

ENTERPRISE = keys().key(tier="enterprise", max_nodes=None, max_precedents=None)


@pytest.fixture
async def ring(nodes: Nodes) -> AsyncIterator[Ring]:
    made = Ring(nodes)
    yield made
    await made.close()


def heads(ring: Ring) -> list[str]:
    return [i for i, d in ring.daemons.items() if d.cluster.role == "head"]


def ready(ring: Ring, *ids: str) -> bool:
    return all(i in ring.daemons and ring.daemons[i].cluster.ready()[0] for i in ids)


def watch(d: Any) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    emit = d.cluster.audit.emit

    def kept(kind: str, **detail: Any) -> None:
        seen.append({"kind": kind, **{k: v for k, v in detail.items() if k != "who"}})
        emit(kind, **detail)

    d.cluster.audit.emit = kept
    return seen


async def three(ring: Ring) -> list[str]:
    ids = ["n0", "n1", "n2"]
    head = await ring.add("n0", ids, ENTERPRISE)
    await until(lambda: head.cluster.role == "head")
    tickets(head)
    status, body, _ = await ring.call("n0", "record", ticket(1), outcome="page oncall")
    assert (status, body["status"]) == (200, "done"), body
    for n in ids[1:]:
        await ring.add(n, ids, ENTERPRISE)
    await until(lambda: ready(ring, *ids) and rows(ring.daemons["n2"]) == 1)
    return ids


async def test_the_head_lost_another_voter_is_elected_and_the_old_head_follows_when_back(
    ring: Ring, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(replication, "TIMING", Timing(heartbeat=0.05, election_min=1.0, election_max=1.5))
    ids = await three(ring)
    one, two = ring.daemons["n1"], ring.daemons["n2"]
    seen = {n: watch(ring.daemons[n]) for n in ("n1", "n2")}
    await until(lambda: len(one.cluster.view()["members"]) == 3)
    _, view = await ring.get("n1", "/v1/cluster")
    assert view["ha"] is True and [m["voter"] for m in view["members"]] == [True, True, True]
    await ring.stop("n0")
    await until(lambda: one.cluster.head_channel is None)
    status, body, headers = await ring.call("n1", "record", ticket(2), outcome="x")
    assert (status, body["error"]["code"], headers["Retry-After"]) == (503, "E_NO_HEAD", "2")  # an election is on
    await until(lambda: len(heads(ring)) == 1, within=10.0)
    new = heads(ring)[0]
    other = "n2" if new == "n1" else "n1"
    assert ring.daemons[new].cluster.raft.term >= 2
    assert [e["reason"] for e in seen[new] if e["kind"] == "cluster.head"] == ["elected"]
    await until(lambda: ready(ring, other) and ring.daemons[other].cluster.state.get("head") == new)
    status, body, _ = await ring.call(other, "record", ticket(3), outcome="page oncall")  # writes resume
    assert (status, body["status"]) == (200, "done") and body["run_id"].startswith(f"{new}~"), body
    await until(lambda: rows(one) == rows(two) == 2)
    zero = await ring.add("n0", ids, ENTERPRISE)  # the old head, restarted: it follows whoever was elected
    await until(lambda: ready(ring, "n0") and zero.cluster.role == "follower" and rows(zero) == 2)
    assert heads(ring) == [new]


async def test_a_head_out_of_touch_answers_not_committed_steps_down_and_what_it_kept_is_committed_later(
    ring: Ring, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(replication, "TIMING", Timing(heartbeat=0.05, election_min=4.0, election_max=5.0))
    monkeypatch.setattr(cluster_node, "COMMIT_WITHIN", 0.3)
    ids = await three(ring)
    zero = ring.daemons["n0"]
    seen = watch(zero)
    await ring.stop("n1")
    await ring.stop("n2")
    status, body, _ = await ring.call("n0", "record", ticket(2), outcome="kept")
    assert (status, body["error"]["code"]) == (503, "E_NOT_COMMITTED"), body
    assert rows(zero) == 2  # written, and kept in its log
    await until(lambda: zero.cluster.role == "follower")
    assert [e["reason"] for e in seen if e["kind"] == "cluster.head"] == ["no_majority"]
    status, body, headers = await ring.call("n0", "record", ticket(3), outcome="x")
    assert (status, body["error"]["code"], headers["Retry-After"]) == (503, "E_NO_HEAD", "2")
    one = await ring.add("n1", ids, ENTERPRISE)  # a majority again: n0's log is the longer, so n0 is elected
    await until(lambda: heads(ring) == ["n0"] and ready(ring, "n1") and rows(one) == 2, within=20.0)
    with pytest.raises(RrotorError) as caught:
        await zero.cluster.committed(zero.cluster.raft.term - 1)  # a write of an earlier term: deposed meanwhile
    assert caught.value.code == "E_NO_HEAD" and caught.value.headers == {"Retry-After": "2"}


async def test_promotion_under_ha_moves_the_head_only_to_an_up_to_date_voter(ring: Ring):
    await three(ring)
    code, said = await command.run(ring.daemons["n1"].config, ["promote", "n1", "--force"])
    assert code == 1 and "E_CLUSTER_HA" in said
    code, said = await command.run(ring.daemons["n1"].config, ["promote", "n1"])
    assert code == 0 and json.loads(said)["head"] == "n1"  # the head answers, and steps down as asked
    await until(lambda: heads(ring) == ["n1"] and ready(ring, "n0", "n2"))
    status, body, _ = await ring.call("n0", "record", ticket(4), outcome="x")
    assert (status, body["status"]) == (200, "done") and body["run_id"].startswith("n1~"), body


async def test_voters_are_the_first_five_and_a_learner_is_made_one_when_a_voter_leaves(nodes: Nodes):
    ids = [f"m{i}" for i in range(6)]
    made = [nodes.make(i, ids, tier="enterprise", max_nodes=None) for i in ids]
    for c in made:
        await c.start()
    head = made[0]
    await until(lambda: all(len(c.raft.ids()) == 6 and c.status == "ready" for c in made), within=15.0)
    assert head.raft.voters() == ids[:5] and not head.view()["members"][5]["voter"]
    with pytest.raises(RrotorError) as caught:
        await made[5]._command({"kind": "command"}, {"name": "promote", "node": "m5"})
    assert caught.value.code == "E_CLUSTER_HA"  # a learner is never head
    assert made[5].raft.tick(10**9) is None  # nor does it stand
    await head._command({"kind": "command"}, {"name": "leave", "node": "m2"})
    await until(lambda: head.raft.voters() == ["m0", "m1", "m3", "m4", "m5"])
    await until(lambda: made[5].raft.voters() == head.raft.voters())
