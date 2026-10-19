"""A deterministic simulation of the cluster's consensus (docs/specs/cluster.md, 4 and 11): the nodes' Raft
(rrotord/cluster/raft.py) over a seeded network in one thread, with no clock but the scheduler's. From one seed: when
each message arrives (delays, so reordering), which are lost or delivered twice, partitions and their healing,
crashes and restarts (a crashed node keeps its log, its term and vote, and its store), writes, members joining and
leaving, compaction of the log, and (without HA) an operator's promotions, forced or not.

Every node holds what a real one holds: the log, the saved term, vote, head and members, and a store (`state`, the
entries applied in order). A head applies what it proposes at once, as the head's runs write their store before the
entry is logged; a follower applies what is committed; a snapshot is the head's whole store.

While it runs it checks, after every event: at most one head per term; no node's term ever goes down; no node accepts
an append of a lower term; every entry committed anywhere is the same entry wherever else it is committed or applied.
At the end it heals the network, restarts every node, lets a head emerge (with HA by election; without, by an
operator's promotion if need be), and checks that every member of the head's configuration holds the head's store
and log, and that every entry ever committed is in it."""

from __future__ import annotations

import heapq
import itertools
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rrotord.cluster.raft import Raft, Timing, majority

TIMING = Timing(heartbeat=0.05, election_min=0.15, election_max=0.3)
TICK = 0.01


class MemLog:
    """The log in memory: the entries after `base`, whose term is `base_term`."""

    def __init__(self) -> None:
        self.base, self.base_term = 0, 0
        self.entries: list[dict[str, Any]] = []

    @property
    def last(self) -> int:
        return self.base + len(self.entries)

    def term_at(self, seq: int) -> int | None:
        if seq == self.base:
            return self.base_term
        if self.base < seq <= self.last:
            return int(self.entries[seq - self.base - 1]["term"])
        return None

    def entry(self, seq: int) -> dict[str, Any]:
        return self.entries[seq - self.base - 1]

    def read(self, after: int, limit: int = 100) -> list[dict[str, Any]]:
        start = max(after, self.base) - self.base
        return [dict(e) for e in self.entries[start : start + limit]]

    def append(self, entry: dict[str, Any]) -> dict[str, Any]:
        seq = entry.setdefault("seq", self.last + 1)
        assert seq == self.last + 1, (seq, self.last)
        self.entries.append(dict(entry))
        return entry

    def truncate(self, after: int) -> None:
        assert after >= self.base, (after, self.base)
        del self.entries[after - self.base :]

    def reset(self, base: int, term: int) -> None:
        self.base, self.base_term, self.entries = base, term, []

    def trim(self, upto: int) -> None:
        """Compaction: what is applied goes (as the real log's oldest segments do)."""
        if self.base < upto <= self.last:
            self.base_term = self.term_at(upto) or 0
            self.entries = self.entries[upto - self.base :]
            self.base = upto


@dataclass
class Node:
    id: str
    log: MemLog = field(default_factory=MemLog)
    saved: dict[str, Any] = field(default_factory=dict)
    state: list[tuple[int, int, str, Any]] = field(default_factory=list)  # the store: (seq, term, kind, value)
    alive: bool = True
    life: int = 0  # incarnation: an answer meant for an earlier one is never delivered
    core: Raft | None = None
    beat: float = 0.0
    promoting_until: float = 0.0
    seen_commit: int = 0


class Violation(AssertionError):
    pass


class World:
    def __init__(self, seed: int, ha: bool, nodes: int = 3, seconds: float = 3.0) -> None:
        self.seed, self.ha, self.seconds = seed, ha, seconds
        self.rng = random.Random(seed)
        self.now = 0.0
        self.queue: list[tuple[float, int, Callable[[], None]]] = []
        self.order = itertools.count()
        self.nodes: dict[str, Node] = {}
        self.group: dict[str, int] = {}
        self.loss = self.rng.choice([0.0, 0.02, 0.1])
        self.dup = self.rng.choice([0.0, 0.02])
        self.delay = self.rng.choice([(0.001, 0.005), (0.001, 0.03), (0.005, 0.08)])
        self.heads: dict[int, str] = {}
        self.terms: dict[str, int] = {}
        self.committed: dict[int, tuple[int, str, Any]] = {}
        self.values = itertools.count(1)
        self.stats = {"elections": 0, "promotions": 0, "snapshots": 0, "truncations": 0, "crashes": 0}
        self.batch = self.rng.choice([1, 2, 5, 50])
        self.every = self.rng.choice([0.01, 0.03, 0.08])  # the mean time between two things happening
        names = ["write", "crash", "crash_head", "partition", "isolate", "trim", "join", "leave"]
        self.weights = {name: self.rng.choice([0, 1, 3, 8]) for name in names}
        self.weights["write"] += 4
        if not ha:
            self.weights["promote"] = self.rng.choice([1, 3])
        first = self.add("n0")
        assert first.core is not None
        first.core.bootstrap({"node_id": "n0"}, self.now)
        self.settle(first)
        for i in range(1, nodes):
            self.join(f"n{i}")

    # Nodes

    def add(self, nid: str) -> Node:
        node = Node(nid)
        self.nodes[nid] = node
        self.group[nid] = 0
        self.boot(node)
        return node

    def boot(self, node: Node) -> None:
        def save(values: dict[str, Any]) -> None:
            node.saved.update(values)

        node.core = Raft(
            node.id,
            node.log,
            dict(node.saved),
            ha=lambda: self.ha,
            save=save,
            now=self.now,
            applied=len(node.state),
            rng=random.Random(self.rng.random()),
            timing=TIMING,
            batch=self.batch,
        )
        node.seen_commit = 0
        if not self.ha and node.saved.get("head") == node.id:
            node.core.resume(self.now)
            self.settle(node)

    def join(self, nid: str) -> bool:
        head = self.head()
        if head is None or not head.core or not head.core.ready_for_change() or nid in head.core.ids():
            return False
        if nid not in self.nodes:
            self.add(nid)
        head.core.add({"node_id": nid}, self.now)
        self.settle(head)
        return True

    def leave(self, nid: str) -> bool:
        head = self.head()
        if head is None or not head.core or not head.core.ready_for_change() or nid == head.id:
            return False
        if nid not in head.core.ids():
            return False
        head.core.remove(nid)
        self.settle(head)
        return True

    def head(self) -> Node | None:
        """The live head of the highest term, if any."""
        found = [n for n in self.nodes.values() if n.alive and n.core and n.core.role == "head"]
        return max(found, key=lambda n: n.core.term if n.core else 0, default=None)

    def crash(self, node: Node) -> None:
        node.alive, node.core = False, None
        node.life += 1
        self.stats["crashes"] += 1

    def restart(self, node: Node) -> None:
        if not node.alive:
            node.alive = True
            self.boot(node)

    # The store

    def settle(self, node: Node) -> None:
        """Apply what this node may: a head all it holds, a follower what is committed. Then the checks."""
        core = node.core
        assert core is not None
        upto = node.log.last if core.role == "head" else min(core.commit, node.log.last)
        while len(node.state) < upto:
            seq = len(node.state) + 1
            entry = node.log.entry(seq)
            item = (seq, int(entry["term"]), str(entry["kind"]), entry.get("value"))
            if core.role != "head" and self.ha:
                self.check_committed(seq, item[1:])
            node.state.append(item)
        core.applied = len(node.state)
        self.check(node)

    def check_committed(self, seq: int, item: tuple[int, str, Any]) -> None:
        known = self.committed.setdefault(seq, item)
        if known != item:
            raise Violation(f"seed {self.seed}: seq {seq} committed as {known} and as {item}")

    def check(self, node: Node) -> None:
        core = node.core
        assert core is not None
        if core.role == "head":
            held = self.heads.setdefault(core.term, node.id)
            if held != node.id:
                raise Violation(f"seed {self.seed}: two heads in term {core.term}: {held} and {node.id}")
        if core.term < self.terms.get(node.id, 0):
            raise Violation(f"seed {self.seed}: {node.id}'s term went down")
        self.terms[node.id] = core.term
        if self.ha and core.commit > node.seen_commit:
            for seq in range(max(node.seen_commit, node.log.base) + 1, min(core.commit, node.log.last) + 1):
                entry = node.log.entry(seq)
                self.check_committed(seq, (int(entry["term"]), str(entry["kind"]), entry.get("value")))
        node.seen_commit = max(node.seen_commit, core.commit)

    # The network

    def at(self, delay: float, work: Callable[[], None]) -> None:
        heapq.heappush(self.queue, (self.now + delay, next(self.order), work))

    def reachable(self, a: str, b: str) -> bool:
        return self.group.get(a) == self.group.get(b)

    def send(self, src: Node, dst: str, msg: dict[str, Any]) -> None:
        if self.rng.random() < self.loss:
            return
        copies = 2 if self.rng.random() < self.dup else 1
        for _ in range(copies):
            self.at(self.rng.uniform(*self.delay), lambda: self.deliver(src.id, src.life, dst, msg))

    def deliver(self, src: str, life: int, dst: str, msg: dict[str, Any]) -> None:
        node = self.nodes[dst]
        if not node.alive or not self.reachable(src, dst) or node.core is None:
            return
        core = node.core
        kind = msg["type"]
        if kind == "answer":
            if node.life != msg["life"]:
                return  # the connection it was asked on is gone
            asked, reply = msg["asked"], msg["reply"]
            if asked["type"] == "vote":
                core.voted_for(src, asked, reply, self.now)
            else:
                core.answered(src, asked, reply, self.now)
                at = core.next.get(src) if core.role == "head" else None
                if at is not None and at <= node.log.last:
                    self.ship(node, src)  # the shipper goes on at once while there is more to send
            self.settle(node)
            return
        if kind == "append":
            before = core.term
            reply = core.on_append(msg, self.now)
            if reply["accepted"] and msg["term"] < before:
                raise Violation(f"seed {self.seed}: {dst} took an append of term {msg['term']} below {before}")
            if reply.get("snapshot"):
                self.stats["snapshots"] += 1
        elif kind == "snapshot":
            got = core.on_snapshot(msg, self.now)
            fetched = self.fetch(src, dst, msg) if got is None else None
            if fetched is not None:
                node.state = list(fetched["state"])
                reply = core.installed(
                    fetched["seq"], fetched["term"], fetched["members"], fetched["members_at"], msg["commit_seq"]
                )
            else:
                reply = got or {"term": core.term, "accepted": False}
        else:
            reply = core.on_vote(msg, self.now)
        self.settle(node)
        self.send(node, src, {"type": "answer", "asked": msg, "reply": reply, "life": life})

    # What each node does as time passes

    def tick(self, node: Node) -> None:
        core = node.core
        if not node.alive or core is None:
            return
        if core.tick(self.now) == "campaign":
            self.stats["elections"] += 1
            self.ask_votes(node, core.campaign(self.now))
        if core.role == "candidate" and not self.ha and self.now >= node.promoting_until:
            core.abandon()
        if core.role == "head" and core.ready_for_change() and core.fill() is not None:
            self.settle(node)
        if core.role == "head" and self.now >= node.beat:
            node.beat = self.now + TIMING.heartbeat
            for peer in core.ids():
                if peer != node.id:
                    self.ship(node, peer)
        self.settle(node)

    def ship(self, node: Node, peer: str) -> None:
        """The head's next request of a follower."""
        assert node.core is not None
        self.send(node, peer, node.core.message_for(peer))

    def fetch(self, head: str, by: str, asked: dict[str, Any]) -> dict[str, Any] | None:
        """A snapshot as the follower fetches it when asked: the head's whole store as it is then, from a head
        still of the term that asked (`GET /cluster/v1/snapshot`); None when it cannot be had."""
        node = self.nodes[head]
        core = node.core
        if not node.alive or core is None or core.role != "head" or core.term != asked["term"]:
            return None
        if not self.reachable(head, by) or self.rng.random() < self.loss:
            return None
        return {
            "state": list(node.state),
            "seq": node.log.last,
            "term": node.log.term_at(node.log.last),
            "members": list(core.members),
            "members_at": list(core.members_at),
        }

    def ask_votes(self, node: Node, asked: dict[str, Any]) -> None:
        assert node.core is not None
        for peer in node.core.quorum():
            if peer != node.id:
                self.send(node, peer, asked)
        self.settle(node)

    def promote(self, node: Node, force: bool) -> bool:
        """`rrotord cluster promote` on `node`: a majority of members reachable, and a lagging node only forced."""
        core = node.core
        if core is None or core.role == "head" or node.id not in core.ids():
            return False
        reachable = [
            self.nodes[m] for m in core.ids() if m in self.nodes and self.nodes[m].alive and self.reachable(m, node.id)
        ]
        if len(reachable) < majority(len(core.ids())):
            return False
        mine = (core.last_term, node.log.last)
        if not force and any((n.core.last_term, n.log.last) > mine for n in reachable if n.core):
            return False
        self.stats["promotions"] += 1
        node.promoting_until = self.now + 0.5
        self.ask_votes(node, core.campaign(self.now, promote=True, force=force))
        return True

    # Running

    def chaos(self) -> None:
        """One random thing happening to the cluster, by this seed's weights."""
        what = self.rng.choices(list(self.weights), weights=list(self.weights.values()))[0]
        ids = sorted(self.nodes)
        pick = self.nodes[self.rng.choice(ids)]
        head = self.head()
        if what == "write":
            for _ in range(self.rng.randint(1, 3)):
                if head is not None and head.core is not None:
                    head.core.propose({"kind": "rows", "value": next(self.values)})
                    self.settle(head)
        elif what in ("crash", "crash_head"):
            victim = head if what == "crash_head" and head is not None else pick
            if victim.alive and sum(n.alive for n in self.nodes.values()) > 1:
                self.crash(victim)
                self.at(self.rng.uniform(0.02, 1.0), lambda: self.restart(victim))
        elif what == "partition":
            for nid in ids:
                self.group[nid] = self.rng.randrange(2)
            self.at(self.rng.uniform(0.1, 1.0), self.heal)
        elif what == "isolate":
            self.group[pick.id] = 9
            self.at(self.rng.uniform(0.05, 0.6), self.heal)
        elif what == "trim":
            if pick.alive:
                pick.log.trim(self.rng.randint(pick.log.base, len(pick.state)))
        elif what == "join":
            if len(self.nodes) < 7:
                self.join(f"n{len(self.nodes)}")
        elif what == "leave":
            self.leave(pick.id)
        elif pick.alive:
            self.promote(pick, force=self.rng.random() < 0.3)

    def heal(self) -> None:
        for nid in self.group:
            self.group[nid] = 0

    def step(self) -> bool:
        if not self.queue:
            return False
        when, _, work = heapq.heappop(self.queue)
        self.now = max(self.now, when)
        work()
        return True

    def ticking(self, until: float) -> None:
        def tick_all() -> None:
            for node in list(self.nodes.values()):
                self.tick(node)
            if self.now + TICK < until:
                self.at(TICK, tick_all)

        self.at(TICK, tick_all)

    def run(self) -> World:
        end = self.seconds
        self.ticking(end)
        t = 0.0
        while t < end:
            t += self.rng.expovariate(1 / self.every)
            self.at(t, self.chaos)
        while self.queue and self.now < end:
            self.step()
        self.finish()
        return self

    def finish(self) -> None:
        """Heal everything, bring every node back, let one head emerge, and check every member holds its store."""
        self.queue.clear()
        self.heal()
        self.loss, self.dup = 0.0, 0.0
        for node in self.nodes.values():
            self.restart(node)
        until = self.now + 30.0
        self.ticking(until)
        done = False
        while self.now < until and not done and self.step():
            head = self.head()
            if (
                head is None
                and not self.ha
                and not any(n.core and n.core.role == "candidate" for n in self.nodes.values())
            ):
                best = max(
                    (n for n in self.nodes.values() if n.core and n.id in n.core.ids()),
                    key=lambda n: (n.core.last_term, n.log.last) if n.core else (0, 0),
                )
                self.promote(best, force=True)
            if head is not None and head.core is not None:
                if not any(item[3] == "last" for item in head.state):
                    head.core.propose({"kind": "rows", "value": "last"})
                    self.settle(head)
                done = self.converged(head, quiet=True)
        head = self.head()
        if head is None or head.core is None:
            raise Violation(f"seed {self.seed}: no head at the end")
        if not self.converged(head):
            raise Violation(f"seed {self.seed}: the members did not converge")
        if not any(item[3] == "last" for item in head.state):
            raise Violation(f"seed {self.seed}: the last write did not reach the head's store")
        for seq, item in self.committed.items():
            if seq > len(head.state) or head.state[seq - 1][1:] != item:
                raise Violation(f"seed {self.seed}: committed seq {seq} {item} lost")

    def converged(self, head: Node, quiet: bool = False) -> bool:
        assert head.core is not None
        members = [self.nodes[m] for m in head.core.ids()]
        heads = [n.id for n in self.nodes.values() if n.core and n.core.role == "head"]
        ok = heads == [head.id] and all(
            n.state == head.state and n.log.last == head.log.last and n.core and n.core.commit == head.core.commit
            for n in members
        )
        if not ok and not quiet:
            detail = {
                n.id: (n.core.role if n.core else None, n.log.last, len(n.state), n.core.commit if n.core else 0)
                for n in self.nodes.values()
            }
            print(f"seed {self.seed}: head {head.id} term {head.core.term}; {detail}")
        return ok
