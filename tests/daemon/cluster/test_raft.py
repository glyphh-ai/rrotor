"""Raft's rules (rrotord/cluster/raft.py), one scripted case each: the cases the seeded simulation reaches only by
luck (an old term's entry counted by a new head, a configuration taken back with a truncated entry, a stale answer)
and every refusal, so each rule is shown to hold on its own."""

from __future__ import annotations

import random
from typing import Any

from rrotord.cluster.raft import VOTERS, Raft, Timing, at, flagged
from tests.daemon.cluster.sim import MemLog

T = Timing(heartbeat=0.05, election_min=1.0, election_max=2.0)


class Node(Raft):
    log: MemLog
    kept: dict[str, Any]


def node(me: str, ha: bool = True, saved: dict[str, Any] | None = None, log: MemLog | None = None) -> Node:
    kept: dict[str, Any] = {}
    raft = Node(
        me, log or MemLog(), saved or {}, ha=lambda: ha, save=kept.update, now=0.0, rng=random.Random(1), timing=T
    )
    raft.kept = kept
    return raft


def members(*ids: str, voters: int = VOTERS) -> list[dict[str, Any]]:
    return [{"node_id": n, "voter": i < voters} for i, n in enumerate(ids)]


def cluster(*ids: str, ha: bool = True) -> dict[str, Node]:
    """A head (the first) with every other node a follower holding its log."""
    head = node(ids[0], ha)
    head.bootstrap({"node_id": ids[0]}, 0.0)
    for n in ids[1:]:
        head.add({"node_id": n}, 0.0)
        head.commit = head.log.last  # as if each change were held by the others at once
    rafts: dict[str, Node] = {ids[0]: head}
    for n in ids[1:]:
        follower = node(n, ha)
        sync(head, follower)
        rafts[n] = follower
    return rafts


def sync(head: Raft, follower: Raft, now: float = 0.0) -> dict[str, Any]:
    """The head's append to a follower until it is in step (or refused for good), its answers given back."""
    for _ in range(50):
        asked = head.message_for(follower.me)
        assert asked["type"] == "append"
        reply = follower.on_append(asked, now)
        head.answered(follower.me, asked, reply, now)
        if reply.get("snapshot") or (reply["accepted"] and reply["match"] == head.log.last):
            return reply
    raise AssertionError("not in step")


def test_a_head_counts_an_old_terms_entry_committed_only_with_one_of_its_own():
    """Raft's figure 8: s1, head at term 2, wrote seq w to s2 alone; head again at term 4, it gets w onto s3 too, a
    majority, which must not commit it (a candidate with a term-3 entry at w could still be elected with s4 and s5
    and overwrite it). It is committed once an entry of term 4 is on a majority, and w with it."""
    s1 = cluster("s1", "s2", "s3", "s4", "s5")["s1"]
    base = s1.log.last
    s1.commit = base
    s1.campaign(0.0)
    for n in ("s2", "s3"):
        s1.voted_for(n, {"term": 2}, {"term": 2, "granted": True}, 0.0)
    assert (s1.role, s1.term) == ("head", 2)
    w = s1.propose({"kind": "rows", "value": "w"})["seq"]
    s1.term = 4
    s1.propose({"kind": "head", "node": "s1"})
    s1.match = {"s2": w, "s3": w, "s4": base, "s5": base}
    s1._advance()
    assert s1.commit == base
    s1.match["s2"] = s1.match["s3"] = s1.log.last
    s1._advance()
    assert s1.commit == s1.log.last > w


def test_a_follower_drops_a_tail_it_did_not_apply_and_takes_back_the_members_it_named():
    r = cluster("a", "b", "c")
    a, b = r["a"], r["b"]
    before = list(b.ids())
    b.applied = b.log.last
    seq = b.log.last + 1
    gone: dict[str, Any] = {
        "seq": seq,
        "term": 1,
        "kind": "member",
        "op": "add",
        "node": "x",
        "members": [*b.members, {"node_id": "x", "voter": True}],
    }
    b.on_append(
        {"term": 1, "head": "a", "prev_seq": b.log.last, "prev_term": 1, "entries": [gone], "commit_seq": 0}, 0.0
    )
    assert b.ids() == [*before, "x"] and b.members_at == (seq, 1)
    a.term = 2  # a head of a later term with another entry at that seq
    a.propose({"kind": "head", "node": "a"})
    a.next["b"] = seq
    reply = sync(a, b)
    assert reply["accepted"] and b.ids() == before and b.log.term_at(seq) == 2
    f = node("f")
    joined = {"seq": 1, "term": 1, "kind": "member", "members": members("a", "f")}
    f.on_append(
        {
            "term": 1,
            "head": "a",
            "prev_seq": 0,
            "prev_term": 0,
            "entries": [joined, {"seq": 2, "term": 1}],
            "commit_seq": 0,
        },
        0.0,
    )
    f.on_append(
        {"term": 3, "head": "a", "prev_seq": 1, "prev_term": 1, "entries": [{"seq": 2, "term": 3}], "commit_seq": 0},
        0.0,
    )
    assert f.ids() == ["a", "f"] and f.log.term_at(2) == 3  # a conflict after the members' entry: they stay
    assert f.on_append(
        {"term": 2, "head": "a", "prev_seq": 0, "prev_term": 0, "entries": [], "commit_seq": 0}, 0.0
    ) == {"term": 3, "accepted": False}  # a head of a lower term


def test_a_follower_asks_for_a_snapshot_when_what_conflicts_was_applied_or_is_not_held():
    r = cluster("a", "b")
    a, b = r["a"], r["b"]
    b.applied = b.log.last
    applied = b.log.last
    a.term = 2
    a.log.truncate(applied - 1)  # the head lacks b's last entry and writes another there
    a.propose({"kind": "rows", "value": 1})
    a.next["b"] = applied
    asked = a.message_for("b")
    assert b.on_append(asked, 0.0) == {"term": 2, "accepted": False, "snapshot": True}  # its entry: applied
    a.answered("b", asked, {"term": 2, "accepted": False, "snapshot": True}, 0.0)
    assert a.next["b"] is None and a.message_for("b")["type"] == "snapshot"
    # the previous entry of another term, and applied: a snapshot too
    c = node("c", log=MemLog())
    c.log.append({"term": 1, "kind": "x"})
    c.applied = 1
    assert c.on_append({"term": 3, "head": "a", "prev_seq": 1, "prev_term": 2, "entries": [], "commit_seq": 0}, 0.0)[
        "snapshot"
    ]
    # one not applied: refused with where to begin again
    c.applied = 0
    assert c.on_append(
        {"term": 3, "head": "a", "prev_seq": 1, "prev_term": 2, "entries": [], "commit_seq": 0}, 0.0
    ) == {"term": 3, "accepted": False, "applied": 0}
    # trimmed: an entry this log no longer holds is the head's only if committed (with HA)
    d = node("d")
    for _ in range(3):
        d.log.append({"term": 1, "kind": "x"})
    d.log.trim(2)
    asked = {"term": 1, "head": "a", "prev_seq": 1, "prev_term": 1, "entries": [], "commit_seq": 3}
    assert d.on_append(asked, 0.0)["snapshot"]  # seq 1 is not held, and not known committed
    asked = {"term": 1, "head": "a", "prev_seq": 0, "prev_term": 0, "entries": [{"seq": 1, "term": 1}], "commit_seq": 3}
    assert d.on_append(asked, 0.0)["snapshot"]  # nor is the entry sent
    d.commit = 2
    asked["entries"] = [{"seq": 1, "term": 1}, {"seq": 2, "term": 1}, {"seq": 3, "term": 1}]
    assert d.on_append(asked, 0.0) == {"term": 1, "accepted": True, "match": 3}  # committed: the head's
    pro = node("p", ha=False)
    for _ in range(3):
        pro.log.append({"term": 1, "kind": "x"})
    pro.log.trim(2)
    pro.commit = 3
    assert pro.on_append(asked, 0.0)["snapshot"]  # without HA nothing unheld is known to be the head's
    assert pro.on_append(asked | {"prev_seq": 9}, 0.0) == {"term": 1, "accepted": False, "applied": 0}


def test_a_deposed_head_takes_a_snapshot_before_it_follows():
    r = cluster("a", "b")
    a = r["a"]
    assert a.on_vote({"term": 5, "candidate": "b", "last_seq": 99, "last_term": 4, "promote": True}, 0.0)["granted"]
    assert a.role == "follower" and a.stale and a.kept["stale"]
    asked = {"term": 5, "head": "b", "prev_seq": 0, "prev_term": 0, "entries": [], "commit_seq": 0}
    assert a.on_append(asked, 0.0)["snapshot"]
    assert a.on_snapshot({"term": 4, "head": "x"}, 0.0) == {"term": 5, "accepted": False}
    assert a.on_snapshot({"term": 5, "head": "b"}, 0.0) is None
    got = a.installed(9, 5, members("a", "b"), [7, 5], 4)
    assert got == {"term": 5, "accepted": True, "match": 9} and not a.stale and a.commit == 4
    assert a.log.last == 9 and a.members_at == (7, 5) and a.applied == 9


def test_votes_once_a_term_for_a_log_as_up_to_date_and_a_working_head_is_kept():
    r = cluster("a", "b", "c")
    a, b, c = r["a"], r["b"], r["c"]
    ask = {"term": 2, "candidate": "c", "last_seq": c.log.last, "last_term": c.last_term}
    assert b.on_vote(ask, 0.5) == {"term": 1, "granted": False}  # b heard from its head within election_min
    assert b.on_vote(ask, 5.0) == {"term": 2, "granted": True}
    assert b.on_vote(ask | {"candidate": "a"}, 5.0)["granted"] is False  # one vote a term
    assert b.on_vote(ask, 5.0)["granted"]  # the same candidate asking again
    assert b.on_vote(ask | {"term": 1}, 5.0) == {"term": 2, "granted": False}  # a lower term
    short = {"term": 3, "candidate": "c", "last_seq": 0, "last_term": 0}
    assert b.on_vote(short, 5.0) == {"term": 3, "granted": False}  # a shorter log: never
    assert a.on_vote(ask | {"term": 9}, 0.1)["granted"] is False and a.role == "head"  # in touch with voters
    assert a.on_vote(ask | {"term": 9, "promote": True}, 0.1)["granted"] and a.role == "follower"
    # without HA nobody is sticky; a forced promotion is granted whatever the log, by the same members only
    p = cluster("p", "q", ha=False)
    q = p["q"]
    forced = {"term": 7, "candidate": "z", "last_seq": 0, "last_term": 0, "promote": True, "force": True}
    assert q.on_vote(forced | {"members_at": [99, 1]}, 0.0)["granted"] is False
    assert q.on_vote(forced | {"term": 8, "members_at": list(q.members_at)}, 0.0)["granted"]
    assert q.on_vote(forced | {"term": 9, "promote": False}, 0.0) == {"term": 9, "granted": False}  # not forced


def test_a_candidate_counts_votes_of_its_term_only_and_a_higher_term_ends_it():
    r = cluster("a", "b", "c")
    b = r["b"]
    asked = b.campaign(5.0)
    assert b.role == "candidate" and asked["term"] == 2 and asked["candidate"] == "b"
    b.voted_for("c", {"term": 1}, {"term": 1, "granted": True}, 5.0)  # an answer to an older campaign
    assert b.role == "candidate"
    b.voted_for("c", asked, {"term": 2, "granted": False}, 5.0)
    assert b.role == "candidate"
    b.voted_for("c", asked, {"term": 2, "granted": True}, 5.0)
    assert b.role == "head" and b.head == "b"
    b.voted_for("a", asked, {"term": 2, "granted": True}, 5.0)  # late: it is head already
    c = r["c"]
    c.campaign(5.0)
    c.voted_for("a", {"term": c.term}, {"term": 9, "granted": False}, 5.0)
    assert (c.role, c.term) == ("follower", 9)
    c.campaign(6.0)
    c.abandon()
    assert c.role == "follower"
    c.abandon()  # not a candidate: nothing


def test_one_voter_is_head_at_once_and_a_learner_never_stands():
    solo = node("a")
    solo.members = members("a")
    solo.campaign(0.0)
    assert solo.role == "head"
    learner = node("l")
    learner.members = members("a", "b", "c", "d", "e", "l")
    assert learner.voters() == ["a", "b", "c", "d", "e"] and learner.tick(99.0) is None


def test_a_head_out_of_touch_with_a_majority_of_voters_steps_down_and_a_follower_without_one_stands():
    r = cluster("a", "b", "c")
    a, b = r["a"], r["b"]
    assert a.tick(0.5) is None
    a.heard["b"] = 0.5
    assert a.tick(1.2) is None  # b answered within election_min
    assert a.tick(9.0) == "step_down" and a.role == "follower" and a.stale
    assert b.tick(0.1) is None
    assert b.tick(99.0) == "campaign"
    pro = node("p", ha=False)
    assert pro.tick(99.0) is None


def test_the_heads_shipping_answers_and_what_it_ignores():
    r = cluster("a", "b", "c")
    a = r["a"]
    a.attach("b", None)
    assert a.message_for("b")["type"] == "snapshot"
    a.attach("b", True)
    assert a.next["b"] is None
    a.attach("b", 999)
    asked = a.message_for("b")
    assert asked["prev_seq"] == a.log.last  # capped at what the head holds
    a.answered("b", asked, {"term": 1, "accepted": True, "match": a.log.last}, 1.0)
    assert a.heard["b"] == 1.0 and a.match["b"] == a.log.last
    a.answered("b", asked | {"prev_seq": 0}, {"term": 1, "accepted": False, "applied": 0}, 1.0)
    assert a.next["b"] == a.log.last + 1  # a stale refusal (below what b holds) changes nothing
    a.answered("b", asked | {"term": 0}, {"term": 1, "accepted": True, "match": 1}, 1.0)  # another term's
    a.answered("x", asked, {"term": 1, "accepted": True, "match": 1}, 1.0)  # a node that is not a member
    assert "x" not in a.match
    a.next["c"] = None
    a.match["c"] = 0
    a.answered("c", {"type": "append", "term": 1, "prev_seq": 3}, {"term": 1, "accepted": False}, 1.0)
    assert a.next["c"] is None  # a snapshot is coming: a refusal does not change it
    a.next["c"] = 4
    a.answered("c", {"type": "append", "term": 1, "prev_seq": 3}, {"term": 1, "accepted": False, "applied": 1}, 1.0)
    assert a.next["c"] == 2
    a.answered("c", {"type": "snapshot", "term": 1}, {"term": 1, "accepted": False}, 1.0)
    assert a.next["c"] == 2
    a.answered("c", {"type": "snapshot", "term": 1}, {"term": 1, "accepted": True, "match": 3}, 1.0)
    assert a.match["c"] == 3 and a.next["c"] == 4
    a.remove("c")
    assert "c" not in a.next and "c" not in a.ids()
    a.answered("b", asked, {"term": 4}, 1.0)
    assert (a.role, a.term, a.head) == ("follower", 4, None)
    a.answered("b", asked, {"term": 4, "accepted": True, "match": 1}, 1.0)  # no longer head: ignored


def test_membership_changes_one_vote_at_a_time():
    r = cluster("a", "b", "c", "d", "e", "f")
    a = r["a"]
    assert a.voters() == ["a", "b", "c", "d", "e"] and not a.members[-1]["voter"]
    assert a.fill() is None  # five voters already
    a.add({"node_id": "b", "address": "b:7778"}, 0.0)  # a member's details: its vote kept
    assert a.members[1] == {"node_id": "b", "address": "b:7778", "voter": True}
    a.remove("e", left=True)
    assert a.voters() == ["a", "b", "c", "d"] and a.log.read(a.log.last - 1)[-1]["left"] is True
    entry = a.fill()
    assert entry is not None and entry["op"] == "voter" and entry["node"] == "f" and "f" in a.voters()
    assert a.fill() is None  # no learner left
    a.commit = 0
    assert not a.ready_for_change()  # the last change not held by a majority yet
    a.match = dict.fromkeys(a.ids(), a.log.last)
    a.commit = a.log.last
    assert a.ready_for_change()
    assert not node("z").ready_for_change()


def test_saved_state_from_before_voters_were_flagged_and_what_is_kept():
    saved = {"term": 3, "voted": "a", "head": "a", "members": [{"node_id": n} for n in "abcdef"], "members_at": "x"}
    raft = node("a", saved=saved)
    assert raft.voters() == ["a", "b", "c", "d", "e"] and raft.members_at == (0, 0) and raft.term == 3
    assert flagged([{"node_id": "a", "voter": False}]) == [{"node_id": "a", "voter": False}]
    assert at([4, 2]) == (4, 2) and at(None) == (0, 0) and at([1]) == (0, 0)
    raft.observe(2)
    assert raft.term == 3
    raft.observe(4)
    assert (raft.term, raft.voted) == (4, None)
    pro = node("p", ha=False, saved={"term": 2, "head": "p"})
    pro.log.append({"term": 2, "kind": "rows"})
    pro.resume(0.0)
    assert pro.role == "head" and pro.applied == pro.commit == pro.log.last == 2
