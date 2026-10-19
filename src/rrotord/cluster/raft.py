"""Raft's rules for the cluster's head (docs/specs/cluster.md, 4), with no I/O of its own: terms, votes with the
log-up-to-date rule, appends with (term, seq) matching and the truncation of a diverged tail, commit by a majority of
voters, snapshots, and the membership kept in the log. The node (node.py, replication.py) carries what this asks to
send over the cluster's transport and applies what it commits; the simulation (tests/daemon/cluster/sim.py) carries
it over a seeded network that loses, delays and reorders messages, partitions the nodes and crashes them.

**Terms are the fencing tokens.** Every append, vote and snapshot carries the sender's term. A node refuses anything
from a lower term (answering its own), and takes a higher one at once: a head or candidate that sees one is a
follower from then on. A node votes once per term (`voted`, kept with the term before it answers).

**Two ways to a head** (`ha`, the licence's: Enterprise). With `ha`, a voter that hears nothing from a head for its
election timeout (randomised between `election_min` and `election_max`) stands (`campaign`): the term goes up, it
votes for itself and asks the other voters, who grant a vote only to a log at least as up to date as their own; a
majority of voters makes it head. A head that has not heard from a majority of voters within `election_min` steps
down (`tick`) before any other node can be elected; a node that has heard from its head within `election_min`
ignores a candidate (so a node cut off and back cannot depose a working head). Without `ha` (Pro, Team) nobody
stands on their own: the head is the bootstrap one until an operator's `promote` asks every member for its vote at
the next term (`force`: granted whatever the log), and a majority of members makes it head.

**The log.** A head appends a `head` entry when it takes over; its entries are committed once a majority of voters
hold them, one of them of its own term (`ha`), or once on its disk (without). A follower takes an append only if its
log holds the entry before it at the term the head says (`prev_seq`, `prev_term`); a conflicting entry and all
after it go (`truncate`) unless this node already applied it, which only a snapshot undoes (`snapshot` in the
answer): the head's rows were written to its store before they were logged, so a head that was deposed has applied
what nobody committed. Entries the follower already applied match the head's (they were committed), so a refusal
answers the head with `applied`, where it may begin again.

**Membership** is in the log: a `member` entry carries the members after the change, in order of joining. A node
takes a configuration as soon as it holds its entry (and goes back to the one before if that entry is truncated);
the head proposes a change only once the last one and an entry of its own term are committed. **Voters** are the
first `VOTERS` members; the rest are learners, which follow and never count."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

VOTERS = 5
BATCH = 200


class Log(Protocol):
    """What Raft needs of a log (cluster/log.py, and the simulation's in memory)."""

    base: int  # the seq just before the first entry held

    @property
    def last(self) -> int: ...

    def term_at(self, seq: int) -> int | None: ...

    def read(self, after: int, limit: int = ...) -> list[dict[str, Any]]: ...

    def append(self, entry: dict[str, Any]) -> dict[str, Any]: ...

    def truncate(self, after: int) -> None: ...

    def reset(self, base: int, term: int) -> None: ...


@dataclass(frozen=True, slots=True)
class Timing:
    heartbeat: float = 0.25
    election_min: float = 1.5
    election_max: float = 3.0


def majority(n: int) -> int:
    return n // 2 + 1


def ids(members: list[dict[str, Any]]) -> list[str]:
    return [str(m["node_id"]) for m in members]


def flagged(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Members saved before voters were flagged: the first VOTERS by join vote."""
    if any("voter" in m for m in members):
        return members
    return [{**m, "voter": i < VOTERS} for i, m in enumerate(members)]


def at(value: Any) -> tuple[int, int]:
    """A configuration's (seq, term), as saved."""
    if isinstance(value, list | tuple) and len(value) == 2 and all(isinstance(v, int) for v in value):
        return int(value[0]), int(value[1])
    return 0, 0


class Raft:
    def __init__(
        self,
        me: str,
        log: Log,
        saved: dict[str, Any],
        *,
        ha: Callable[[], bool],
        save: Callable[[dict[str, Any]], None],
        now: float,
        applied: int = 0,
        rng: random.Random | None = None,
        timing: Timing = Timing(),  # noqa: B008  (frozen)
        batch: int = BATCH,
    ) -> None:
        self.me, self.log, self.ha, self.save, self.timing, self.batch = me, log, ha, save, timing, batch
        self.rng = rng or random.Random()  # noqa: S311  (timeouts, not secrets)
        self.term = int(saved.get("term") or 0)
        voted = saved.get("voted")
        self.voted: str | None = voted if isinstance(voted, str) else None
        head = saved.get("head")
        self.head: str | None = head if isinstance(head, str) else None
        self.members = flagged(list(saved.get("members") or []))
        self.members_at = at(saved.get("members_at"))  # the (seq, term) of the entry that made them; (0, 0): none
        self.members_before = flagged(list(saved.get("members_before") or self.members))
        self.before_at = at(saved.get("before_at"))
        self.stale = bool(saved.get("stale"))  # this node's stores may hold writes no log has: a snapshot first
        self.role = "follower"  # follower, candidate or head
        self.commit = 0
        self.applied = applied
        self.head_seq = 0  # the head's: its `head` entry, which commits its term
        self.via = ""  # how it became head: bootstrap, resume or votes
        self.next: dict[str, int | None] = {}  # the head's: the seq to send each follower next (None: a snapshot)
        self.match: dict[str, int] = {}  # the head's: what each follower's log holds as its own
        self.heard: dict[str, float] = {}  # the head's: when each follower last answered
        self.votes: set[str] = set()
        self.heard_head = float("-inf")
        self.deadline = now + self._timeout()

    # What the rest reads

    def ids(self) -> list[str]:
        return ids(self.members)

    def voters(self) -> list[str]:
        return [str(m["node_id"]) for m in self.members if m.get("voter")]

    def quorum(self) -> list[str]:
        """Whose votes make a head: the voters with `ha`; every member for an operator's promotion without."""
        return self.voters() if self.ha() else self.ids()

    @property
    def members_seq(self) -> int:
        return self.members_at[0]

    @property
    def last_term(self) -> int:
        return self.log.term_at(self.log.last) or 0

    def ready_for_change(self) -> bool:
        """Whether the head may propose a membership change: the last one, and an entry of its own term, held by a
        majority of whose votes make a head (committed, with `ha`), so two configurations in use never differ by
        more than one node."""
        held = min(self.commit, self._held(self.quorum()))
        return self.role == "head" and held >= max(self.members_seq, self.head_seq)

    def _timeout(self) -> float:
        return self.rng.uniform(self.timing.election_min, self.timing.election_max)

    def _persist(self) -> None:
        self.save(
            {
                "term": self.term,
                "voted": self.voted,
                "head": self.head,
                "members": self.members,
                "members_at": list(self.members_at),
                "members_before": self.members_before,
                "before_at": list(self.before_at),
                "stale": self.stale,
            }
        )

    def _take(self, term: int) -> None:
        """A higher term heard: this node follows, with no head known and no vote given in it. A head that steps
        down so may have written what no log has (a run in flight): it takes a snapshot before it follows."""
        self.stale = self.stale or self.role == "head"
        self.term, self.voted, self.role, self.head = term, None, "follower", None
        self._persist()

    def observe(self, term: int) -> None:
        """A term heard some other way (a hello's answer, a forward): taken if it is higher."""
        if term > self.term:
            self._take(term)

    # The log, as this node holds it

    def _append(self, entry: dict[str, Any]) -> dict[str, Any]:
        made = self.log.append(entry)
        if made.get("kind") == "member":
            self.members_before, self.before_at = self.members, self.members_at
            self.members, self.members_at = list(made["members"]), (int(made["seq"]), int(made["term"]))
            self._persist()
        return made

    def _truncate(self, after: int) -> None:
        self.log.truncate(after)
        self.commit = min(self.commit, after)
        if self.members_seq > after:
            self.members, self.members_at = self.members_before, self.before_at
            self._persist()

    # Becoming the head

    def bootstrap(self, member: dict[str, Any], now: float) -> None:
        """The first node makes the cluster: term 1, itself its only member and its head."""
        self.term, self.voted = 1, self.me
        joined = [{**member, "voter": True}]
        made = self._append({"term": self.term, "kind": "member", "op": "add", "node": self.me, "members": joined})
        self.applied = made["seq"]
        self._become_head(now, "bootstrap")

    def resume(self, now: float) -> None:
        """A head without `ha` restarted: it leads again in the term it led, which only it can lead."""
        self.applied = max(self.applied, self.log.last)
        self._become_head(now, "resume")

    def _become_head(self, now: float, via: str) -> None:
        self.role, self.head, self.votes, self.via = "head", self.me, set(), via
        self.next, self.match = {}, {}
        self.heard = dict.fromkeys(self.ids(), now)
        self.head_seq = self.propose({"kind": "head", "node": self.me})["seq"]
        self._persist()

    def propose(self, body: dict[str, Any]) -> dict[str, Any]:
        """An entry of the head's (what it wrote, or decided), appended in its term; the head has applied it."""
        entry = self._append({"term": self.term, **body})
        self.applied = entry["seq"]
        self._advance()
        return entry

    def add(self, member: dict[str, Any]) -> dict[str, Any]:
        """A `member` entry adding a node (a voter while there are fewer than VOTERS, else a learner), or changing
        a member's details (its address, its version) with its vote kept."""
        node = str(member["node_id"])
        found = next((m for m in self.members if m["node_id"] == node), None)
        voter = bool(found.get("voter")) if found is not None else len(self.voters()) < VOTERS
        made = {**member, "voter": voter}
        members = [made if m is found else m for m in self.members] if found else [*self.members, made]
        return self._change("add", node, members)

    def remove(self, node: str, **detail: Any) -> dict[str, Any]:
        """A `member` entry removing a node. Its vote goes with it: a learner is made a voter by a change of its
        own (`fill`), so no change moves more than one vote."""
        entry = self._change("remove", node, [m for m in self.members if m["node_id"] != node], **detail)
        for table in (self.next, self.match, self.heard):
            table.pop(node, None)
        return entry

    def fill(self) -> dict[str, Any] | None:
        """With fewer than VOTERS voters, the first learner by join made a voter; None when none is to be."""
        learner = next((m for m in self.members if not m.get("voter")), None)
        if learner is None or len(self.voters()) >= VOTERS:
            return None
        members = [{**m, "voter": True} if m is learner else m for m in self.members]
        return self._change("voter", str(learner["node_id"]), members)

    def _change(self, op: str, node: str, members: list[dict[str, Any]], **detail: Any) -> dict[str, Any]:
        return self.propose({"kind": "member", "op": op, "node": node, "members": members, **detail})

    def _held(self, nodes: list[str]) -> int:
        """The highest seq a majority of `nodes` hold (this head counting its whole log)."""
        held = sorted((self.log.last if v == self.me else self.match.get(v, 0) for v in nodes), reverse=True)
        return held[majority(len(nodes)) - 1] if nodes else 0

    def _advance(self) -> None:
        if not self.ha():
            self.commit = self.log.last
            return
        n = self._held(self.voters())
        if n > self.commit and self.log.term_at(n) == self.term:
            self.commit = n

    # The head: what each follower is sent

    def attach(self, peer: str, applied: Any) -> None:
        """A follower (re)connected: begin from what it says it applied, or with a snapshot when it never did."""
        known = isinstance(applied, int) and not isinstance(applied, bool) and applied > 0
        self.next[peer] = min(applied + 1, self.log.last + 1) if known else None
        self.match.pop(peer, None)

    def message_for(self, peer: str) -> dict[str, Any]:
        """The head's next request of a follower: `append` past what it holds (none when in step: a heartbeat), or
        `snapshot` when the log no longer holds what it needs."""
        at = self.next.setdefault(peer, self.log.last + 1)
        prev = at - 1 if at is not None else None
        prev_term = self.log.term_at(prev) if prev is not None else None
        if prev is None or prev_term is None:
            self.next[peer] = None
            return {"type": "snapshot", "term": self.term, "head": self.me, "commit_seq": self.commit}
        return {
            "type": "append",
            "term": self.term,
            "head": self.me,
            "prev_seq": prev,
            "prev_term": prev_term,
            "entries": self.log.read(prev, self.batch),
            "commit_seq": self.commit,
        }

    def answered(self, peer: str, asked: dict[str, Any], reply: dict[str, Any], now: float) -> None:
        """A follower's answer to an append or a snapshot."""
        term = int(reply.get("term") or 0)
        if term > self.term:
            self._take(term)
            return
        if self.role != "head" or asked.get("term") != self.term or peer not in self.ids():
            return  # stale: of another term, or of a node that left
        self.heard[peer] = now
        if reply.get("snapshot"):
            self.next[peer] = None
            return
        if reply.get("accepted"):
            got = int(reply["match"])
            self.match[peer] = max(self.match.get(peer, 0), got)
            self.next[peer] = max(self.next.get(peer) or 0, self.match[peer] + 1)
            self._advance()
        elif asked["type"] == "append" and asked["prev_seq"] >= self.match.get(peer, 0):
            at = self.next.get(peer)
            hint = int(reply.get("applied") or 0)
            if at is not None:
                self.next[peer] = max(min(at - 1, hint + 1), 1)

    # A follower

    def _follow(self, term: int, head: str, now: float) -> None:
        changed = term > self.term or self.role != "follower" or self.head != head
        if term > self.term:
            self.term, self.voted = term, None
        self.role, self.head = "follower", head
        self.heard_head, self.deadline = now, now + self._timeout()
        if changed:
            self._persist()

    def on_append(self, msg: dict[str, Any], now: float) -> dict[str, Any]:
        """The head's append: `accepted` with what this log now holds as the head's (`match`), refused with
        this node's term (a lower term's head) or `applied` (where to begin again), or `snapshot` (a tail it applied
        conflicts). Commit moves to what the head committed, as far as this log holds it."""
        term = int(msg["term"])
        if term < self.term:
            return {"term": self.term, "accepted": False}
        self._follow(term, str(msg["head"]), now)
        prev, log = int(msg["prev_seq"]), self.log
        if self.stale:
            return self._snapshot()
        if prev > log.last:
            return self._refused()
        held = log.term_at(prev)
        if held is None and not self._committed(prev):
            return self._snapshot()
        if held is not None and held != msg["prev_term"]:
            return self._snapshot() if prev <= self.applied else self._refused()
        entries: list[dict[str, Any]] = list(msg.get("entries") or [])
        for entry in entries:
            seq = int(entry["seq"])
            if seq <= log.last:
                have = log.term_at(seq)
                if have == entry["term"] or (have is None and self._committed(seq)):
                    continue
                if have is None or seq <= self.applied:
                    return self._snapshot()
                self._truncate(seq - 1)
            self._append(dict(entry))
        matched = prev + len(entries)
        self.commit = max(self.commit, min(int(msg.get("commit_seq") or 0), matched))
        return {"term": self.term, "accepted": True, "match": matched}

    def _committed(self, seq: int) -> bool:
        """Whether an entry this log no longer holds is known to be the head's: committed by a majority (`ha`).
        Without `ha` an operator's forced promotion may have lost it, so only a snapshot tells."""
        return self.ha() and seq <= self.commit

    def _refused(self) -> dict[str, Any]:
        return {"term": self.term, "accepted": False, "applied": self.applied}

    def _snapshot(self) -> dict[str, Any]:
        return {"term": self.term, "accepted": False, "snapshot": True}

    def on_snapshot(self, msg: dict[str, Any], now: float) -> dict[str, Any] | None:
        """The head's snapshot request: refused (this node's term) from a lower term; None to go ahead and fetch."""
        term = int(msg["term"])
        if term < self.term:
            return {"term": self.term, "accepted": False}
        self._follow(term, str(msg["head"]), now)
        return None

    def installed(
        self, seq: int, term: int, members: list[dict[str, Any]], members_at: Any, commit: int
    ) -> dict[str, Any]:
        """A snapshot at `seq` (the head's log then, at `term`) is in place: the log begins after it, the members
        those the head had then. What it holds past what the head had committed (`commit`, when it asked) is not
        counted committed here."""
        self.log.reset(seq, term)
        self.members = self.members_before = flagged(list(members))
        self.members_at = self.before_at = at(members_at)
        self.applied, self.stale = seq, False
        self.commit = max(min(self.commit, seq), min(commit, seq))
        self._persist()
        return {"term": self.term, "accepted": True, "match": seq}

    # Votes

    def campaign(self, now: float, promote: bool = False, force: bool = False) -> dict[str, Any]:
        """Stand at the next term: a vote for itself, and the request to send the others in `quorum()`. A quorum
        of one makes it head at once."""
        self.term, self.voted, self.role, self.head = self.term + 1, self.me, "candidate", None
        self.votes = {self.me}
        self.deadline = now + self._timeout()
        self._persist()
        asked = {
            "type": "vote",
            "term": self.term,
            "candidate": self.me,
            "last_seq": self.log.last,
            "last_term": self.last_term,
            "promote": promote,
            "force": force,
            "members_at": list(self.members_at),
        }
        self._count(now)
        return asked

    def abandon(self) -> None:
        """A promotion that did not gather its votes: this node follows again, with no head known."""
        if self.role == "candidate":
            self.role = "follower"

    def _sticky(self, now: float) -> bool:
        """With `ha`: this node has a working head (or is one), so a candidate is ignored."""
        if not self.ha():
            return False
        if self.role == "head":
            return self._in_touch(now)
        return self.head is not None and now - self.heard_head < self.timing.election_min

    def on_vote(self, msg: dict[str, Any], now: float) -> dict[str, Any]:
        term = int(msg["term"])
        promote = bool(msg.get("promote"))
        if term < self.term or (not promote and self._sticky(now)):
            return {"term": self.term, "granted": False}
        if term > self.term:
            self._take(term)
        candidate = str(msg["candidate"])
        current = (int(msg["last_term"]), int(msg["last_seq"])) >= (self.last_term, self.log.last)
        forced = bool(msg.get("force")) and promote and not self.ha() and at(msg.get("members_at")) == self.members_at
        granted = self.voted in (None, candidate) and (current or forced)
        if granted:
            self.voted = candidate
            self.deadline = now + self._timeout()
            self._persist()
        return {"term": self.term, "granted": granted}

    def voted_for(self, peer: str, asked: dict[str, Any], reply: dict[str, Any], now: float) -> None:
        term = int(reply.get("term") or 0)
        if term > self.term:
            self._take(term)
            return
        if self.role != "candidate" or asked["term"] != self.term:
            return
        if reply.get("granted"):
            self.votes.add(peer)
        self._count(now)

    def _count(self, now: float) -> None:
        quorum = self.quorum()
        if self.role == "candidate" and len(self.votes & set(quorum)) >= majority(len(quorum)):
            self._become_head(now, "votes")

    # Time

    def _in_touch(self, now: float) -> bool:
        voters = self.voters()
        recent = [v for v in voters if v == self.me or now - self.heard.get(v, -1e18) < self.timing.election_min]
        return len(recent) >= majority(len(voters))

    def tick(self, now: float) -> str | None:
        """With `ha`: `step_down` (a head out of touch with a majority of voters, now a follower), `campaign` (a
        voter whose election timeout passed: call `campaign`), else None."""
        if not self.ha():
            return None
        if self.role == "head":
            if self._in_touch(now):
                return None
            self.role, self.head, self.stale = "follower", None, True
            self.deadline = now + self._timeout()
            self._persist()
            return "step_down"
        if now >= self.deadline and self.me in self.voters():
            return "campaign"
        return None
