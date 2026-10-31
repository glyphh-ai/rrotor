"""Manual failover (docs/specs/cluster.md, 4 and 11, step 7), daemons of one cluster in one event loop: a follower
promoted by `rrotord cluster promote` takes the writes; the command shows each member's lag and refuses a lagging
node without `--force`, a minority, and a head that does not answer until confirmed; an old head that comes back is
fenced by the new term and discards the tail nobody else has; membership is in the log."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest

from rrotord.cluster import command
from rrotord.cluster import node as cluster_node
from rrotord.cluster.tls import Credentials
from rrotord.cluster.transport import ClusterError, fetch
from rrotord.errors import RrotorError
from tests.daemon.cluster.conftest import SECRET, Nodes, until
from tests.daemon.cluster.ring import Ring, held, models


@pytest.fixture
async def ring(nodes: Nodes) -> AsyncIterator[Ring]:
    made = Ring(nodes)
    yield made
    await made.close()


def ready(ring: Ring, *ids: str) -> bool:
    return all(i in ring.daemons and ring.daemons[i].cluster.ready()[0] for i in ids)


def watch(d: Any) -> list[dict[str, Any]]:
    """The cluster's audit events on a node, as emitted (Pro keeps none: they are log lines)."""
    seen: list[dict[str, Any]] = []
    emit = d.cluster.audit.emit

    def kept(kind: str, **detail: Any) -> None:
        who = detail.get("who")
        seen.append({"kind": kind, **detail, "actor": who.actor if who is not None else None})
        emit(kind, **detail)

    d.cluster.audit.emit = kept
    return seen


def heads(ring: Ring) -> list[str]:
    return [i for i, d in ring.daemons.items() if d.cluster.role == "head"]


async def three(ring: Ring) -> list[str]:
    ids = ["n0", "n1", "n2"]
    head = await ring.add("n0", ids)
    await until(lambda: head.cluster.role == "head")
    models(head)
    status, body, _ = await ring.write("n0")
    assert (status, body["status"]) == (200, "done"), body
    for n in ids[1:]:
        await ring.add(n, ids)
    await until(lambda: ready(ring, *ids) and held(ring.daemons["n2"]) == 1)
    return ids


async def promote(ring: Ring, node: str, *flags: str, on: str | None = None) -> tuple[int, Any]:
    code, said = await command.run(ring.daemons[on or node].config, ["promote", node, *flags])
    return code, json.loads(said) if code == 0 else said


async def test_a_follower_promoted_while_the_head_is_down_takes_the_writes_and_the_old_head_follows(ring: Ring):
    ids = await three(ring)
    one, two = ring.daemons["n1"], ring.daemons["n2"]
    events = watch(one)
    assert [m["voter"] for m in one.cluster.raft.members] == [True, True, True]  # membership: in the log
    kinds = [e["kind"] for e in ring.daemons["n0"].cluster.replication.log.read(0)]
    assert kinds[:2] == ["member", "head"] and kinds.count("member") == 3
    await ring.stop("n0")
    await until(lambda: one.cluster.head_channel is None and two.cluster.head_channel is None)
    status, body, _ = await ring.write("n1")
    assert (status, body["error"]["code"]) == (503, "E_NO_HEAD")
    code, said = await promote(ring, "n1")
    assert code == 1 and "E_CLUSTER_CONFIRM" in said and "--yes" in said and "n0: unreachable" in said
    asked: list[str] = []

    def yes(question: str) -> bool:
        asked.append(question)
        return True

    code, said = await command.run(one.config, ["promote", "n1"], yes)  # at a terminal: asked, and confirmed
    assert code == 0 and asked and "Is it down?" in asked[0]
    done = json.loads(said)
    assert (done["head"], done["term"], done["promoted"], done["forced"]) == ("n1", 2, True, False)
    assert done["members"]["n2"] == {"reachable": True, "last_seq": done["members"]["n1"]["last_seq"], "lag": 0}
    assert one.cluster.role == "head" and one.cluster.view()["term"] == 2
    promoted = [e for e in events if e["kind"] == "cluster.head"]
    assert promoted[-1]["reason"] == "promoted" and promoted[-1]["actor"] == {"type": "local"}
    await until(lambda: two.cluster.role == "follower" and two.cluster.state.get("head") == "n1" and ready(ring, "n2"))
    # writes resume on the promoted node, forwarded from the other follower too
    status, body, _ = await ring.write("n2")
    assert (status, body["status"]) == (200, "done") and body["run_id"].startswith("n1~"), body
    await until(lambda: held(two) == 2 == held(one))
    # the old head comes back: a member it reaches knows term 2, so it follows instead of leading again
    zero = await ring.add("n0", ids)
    await until(lambda: zero.cluster.role == "follower" and ready(ring, "n0") and held(zero) == 2)
    assert heads(ring) == ["n1"] and zero.cluster.raft.term == 2
    code, said = await promote(ring, "n1", on="n2")  # passed to the node named, which is head already
    assert code == 0 and said["promoted"] is False


async def test_a_head_that_answers_hands_over_at_once_and_its_writing_runs_fail(ring: Ring):
    await three(ring)
    zero, one = ring.daemons["n0"], ring.daemons["n1"]
    events = watch(zero)
    stopped: list[str] = []

    async def deposed() -> None:
        stopped.append("runs")

    zero.cluster.deposed = deposed
    code, done = await promote(ring, "n2", on="n1")  # from another node, no confirmation: the head answers
    assert code == 0 and done["head"] == "n2" and done["term"] == 2
    await until(lambda: zero.cluster.role == "follower" and stopped == ["runs"])
    assert zero.cluster.raft.stale or zero.cluster.raft.applied  # it takes the new head's snapshot first
    deposed_events = [e for e in events if e["kind"] == "cluster.head"]
    assert deposed_events[-1]["reason"] == "deposed" and deposed_events[-1]["term"] == 2
    await until(lambda: ready(ring, "n0", "n1") and heads(ring) == ["n2"])
    status, body, _ = await ring.write("n0")
    assert (status, body["status"]) == (200, "done") and body["run_id"].startswith("n2~"), body
    await until(lambda: held(zero) == held(one) == 2)


async def test_a_lagging_node_is_refused_unless_forced_and_the_writes_it_lacks_are_lost(ring: Ring):
    ids = await three(ring)
    await ring.stop("n2")
    _, body, _ = await ring.write("n0")  # only on n0 and n1
    assert body["status"] == "done"
    one = ring.daemons["n1"]
    await until(lambda: held(one) == 2)
    await ring.stop("n0")
    two = await ring.add("n2", ids)  # back, behind n1, with no head to catch up from
    await until(lambda: two.cluster.listener is not None and two.cluster.raft.ids() == ids)
    code, said = await promote(ring, "n2", "--yes")
    assert code == 1 and "E_CLUSTER_LAG" in said and "n1: seq" in said and "lag 0" in said
    code, done = await promote(ring, "n2", "--yes", "--force")
    assert code == 0 and done["forced"] and done["members"]["n2"]["lag"] > 0
    await until(lambda: ready(ring, "n1") and one.cluster.role == "follower" and held(one) == 1)  # lost, as warned
    assert held(two) == 1


async def test_what_a_promotion_refuses(ring: Ring):
    ids = await three(ring)
    two = ring.daemons["n2"]
    code, said = await promote(ring, "nobody", on="n2")
    assert code == 1 and "E_CLUSTER_MEMBER" in said
    code, said = await promote(ring, "n0", on="n0")
    assert code == 0 and said == {"head": "n0", "term": 1, "promoted": False, "reason": "already the head"}

    def refuse(msg: dict[str, Any], _: float) -> dict[str, Any]:
        return {"term": msg["term"], "granted": False}

    ring.daemons["n1"].cluster.raft.on_vote = refuse  # type: ignore[method-assign,assignment]
    ring.daemons["n0"].cluster.raft.on_vote = refuse  # type: ignore[method-assign,assignment]
    code, said = await promote(ring, "n2")
    assert code == 1 and "E_CLUSTER_VOTES" in said and two.cluster.role != "head"
    await ring.stop("n0")
    await ring.stop("n1")
    code, said = await promote(ring, "n2", "--yes")
    assert code == 1 and "E_CLUSTER_QUORUM" in said and "n0: unreachable" in said
    with pytest.raises(RrotorError, match="Name the node"):
        await two.cluster._command({"kind": "command"}, {"name": "promote", "node": "Not_A_Node"})
    assert ids


async def test_a_head_that_hears_of_a_higher_term_steps_down(ring: Ring, monkeypatch: pytest.MonkeyPatch):
    await three(ring)
    zero, one = ring.daemons["n0"], ring.daemons["n1"]
    # a forward from a follower that knows a later term: the head is deposed, the caller told there is no head
    one.cluster.raft.term = 5
    with pytest.raises(RrotorError) as caught:
        await one.cluster.forward("approvals", {})
    assert caught.value.code == "E_NO_HEAD" and zero.cluster.role == "follower" and zero.cluster.raft.term == 5
    # a head with a member it is not connected to asks who it is: a higher term deposes it
    ring2 = ring.daemons["n2"]
    ring2.cluster.raft.observe(5)
    await until(lambda: "n0" in heads(ring) or zero.cluster.raft.role == "follower")

    async def survey() -> tuple[list[dict[str, Any]], bool]:
        return [{"cluster_id": zero.cluster.cluster_id, "term": 9}], True

    zero.cluster.raft.role, zero.cluster.role = "head", "head"
    monkeypatch.setattr(zero.cluster, "_survey", survey)
    zero.cluster.links.clear()
    assert await zero.cluster._member() == cluster_node.RETRY  # deposed: it looks for the head instead of leading
    assert zero.cluster.role == "follower" and zero.cluster.raft.term == 9
    zero.cluster.role = "head"
    zero.cluster.links.update({m.node_id: None for m in zero.cluster.members()})  # type: ignore[misc]
    await zero.cluster._fence()  # every member connected: nobody is asked
    zero.cluster.links.clear()
    zero.cluster.role = "follower"


async def test_membership_changes_wait_for_the_last_one_to_be_held(ring: Ring, monkeypatch: pytest.MonkeyPatch):
    ids = ["n0", "n1", "n2"]
    head = await ring.add("n0", ids)
    await until(lambda: head.cluster.role == "head")
    c, raft = head.cluster, head.cluster.raft
    monkeypatch.setattr(raft, "ready_for_change", lambda: False)
    one = await ring.add("n1", ids)
    await until(lambda: one.cluster.listener is not None)
    await asyncio.sleep(0.2)
    assert one.cluster.role == "standalone" and "n1" not in raft.ids()  # asked again, and again
    monkeypatch.undo()
    await until(lambda: one.cluster.role == "follower" and "n1" in raft.ids())
    monkeypatch.setattr(raft, "ready_for_change", lambda: False)
    with pytest.raises(ClusterError) as caught:
        c._remove("n1", "E_CLUSTER_LEFT", "left", cluster_node.OPERATOR)
    assert (caught.value.code, caught.value.status) == ("E_CLUSTER_BUSY", 409)
    monkeypatch.setattr(cluster_node, "CHANGE_WITHIN", 0.05)
    with pytest.raises(ClusterError) as caught:
        await c._command({"kind": "command"}, {"name": "leave", "node": "n1"})
    assert caught.value.code == "E_CLUSTER_BUSY"
    answers = iter([False, True])
    monkeypatch.setattr(raft, "ready_for_change", lambda: next(answers, True))
    assert await c._command({"kind": "command"}, {"name": "leave", "node": "n1"}) == {"removed": "n1", "members": 1}
    await c._lead("again")  # the head already: nothing to do


async def test_votes_that_cannot_be_asked_and_requests_that_cannot_be_read(ring: Ring, tmp_path):
    await three(ring)
    zero, one = ring.daemons["n0"], ring.daemons["n1"]
    term = zero.cluster.raft.term
    await zero.cluster._vote(cluster_node.Member("x", "127.0.0.1:1", "", "", ""), {"type": "vote", "term": 9})
    await zero.cluster._vote(cluster_node.Member("n1", one.cluster.address, "", "", ""), {"type": "vote", "term": "x"})
    assert zero.cluster.raft.term == term and zero.cluster.role == "head"  # nothing came of either
    sent: list[dict[str, Any]] = []

    class Channel:
        async def send(self, msg: dict[str, Any]) -> None:
            sent.append(msg)

    await one.cluster._answer_head(Channel(), {"type": "append", "id": "1"})  # type: ignore[arg-type]
    assert sent[0]["error"]["code"] == "E_CLUSTER_MESSAGE"
    async with aiohttp.ClientSession() as session:  # a node that is not a member: the head's files are not for it
        with pytest.raises(ClusterError) as caught:
            await fetch(
                session,
                Credentials([SECRET], "x", tmp_path / "x"),
                zero.cluster.address,
                "/cluster/v1/snapshot",
                tmp_path / "s.tar",
                5.0,
            )
        assert caught.value.code == "E_CLUSTER_UNREACHABLE"


async def test_a_bare_node_that_is_deposed_has_no_runs_to_stop(nodes: Nodes):
    a, b = nodes.make("a", ["a", "b"]), nodes.make("b", ["a", "b"])
    await a.start()
    await b.start()
    await until(lambda: b.status == "ready")
    got = a.raft.on_vote({"term": 5, "candidate": "b", "last_seq": 99, "last_term": 1, "promote": True}, 0.0)
    a._reconcile()
    assert got["granted"] and a.role == "follower"
    await until(lambda: not a.telling)
    a._spawn(asyncio.sleep(60))  # whatever it is still telling when it stops is cancelled
    await a.stop()
    assert not a.telling


async def test_a_head_restarted_leads_again_in_its_term_and_a_follower_that_cannot_keep_entries_says_so(
    ring: Ring, monkeypatch: pytest.MonkeyPatch
):
    ids = await three(ring)
    await ring.stop("n0")
    zero = await ring.add("n0", ids)  # no member knows a later term: it leads again, as it was
    two = ring.daemons["n2"]
    await until(lambda: zero.cluster.role == "head" and two.cluster.head_channel is not None and ready(ring, "n2"))
    assert zero.cluster.raft.term == 1 and heads(ring) == ["n0"]
    status, body, _ = await ring.write("n2")
    assert (status, body["status"]) == (200, "done") and body["run_id"].startswith("n0~"), body
    one = ring.daemons["n1"]

    async def full(msg: dict[str, Any]) -> dict[str, Any]:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(one.cluster.replication, "receive", full)
    sent: list[dict[str, Any]] = []

    class Channel:
        async def send(self, msg: dict[str, Any]) -> None:
            sent.append(msg)

    await one.cluster._answer_head(Channel(), {"type": "append", "id": "1"})  # type: ignore[arg-type]
    assert sent[0]["error"]["code"] == "E_CLUSTER_APPLY" and "No space left" in sent[0]["error"]["message"]
