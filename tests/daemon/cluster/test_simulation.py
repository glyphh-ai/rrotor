"""The cluster's consensus under the deterministic simulation (sim.py, docs/specs/cluster.md 11): across thousands of
seeds, partitions, crashes, lost, late and doubled messages, compaction, members joining and leaving, every check
holds: 1,500 seeds without HA and 3,000 with. `RROTOR_SIM_SEEDS` runs more of them."""

from __future__ import annotations

import os
from collections import Counter

import pytest

from rrotord.cluster import raft
from tests.daemon.cluster.sim import Violation, World

SEEDS = int(os.environ.get("RROTOR_SIM_SEEDS", "1500"))


def run(ha: bool, seeds: range) -> Counter[str]:
    seen: Counter[str] = Counter()
    for seed in seeds:
        world = World(seed, ha=ha, nodes=3 if seed % 2 else 5).run()
        seen.update(world.stats)
        seen["committed"] += len(world.committed)
    return seen


def test_manual_failover_keeps_one_head_a_term_fences_lower_terms_and_every_member_converges():
    """Pro and Team: heads only by an operator's promotion (forced or not, with lags and confirmations as the
    command asks), a head restarted leading again in its term until it hears a higher one. Each seed ends with the
    writes resumed on a head and every member holding its store."""
    seen = run(False, range(SEEDS))
    assert seen["promotions"] > SEEDS // 2 and seen["snapshots"] > SEEDS // 2 and seen["crashes"] > SEEDS
    assert seen["elections"] == 0


def test_elected_heads_keep_one_head_a_term_lose_no_committed_entry_and_every_member_converges():
    """Enterprise: voters stand when their head is silent, a majority of voters elects and commits, a head out of
    touch steps down, learners follow. Every entry committed anywhere is the same entry everywhere it is committed
    or applied, and in the head's store at the end."""
    seen = run(True, range(SEEDS * 2))
    assert seen["elections"] > SEEDS * 4 and seen["committed"] > SEEDS * 20 and seen["crashes"] > SEEDS
    assert seen["promotions"] == 0


def test_the_checks_catch_a_broken_rule(monkeypatch: pytest.MonkeyPatch):
    """The simulation is not blind: a head made by any one vote is two heads in a term within a few seeds."""
    monkeypatch.setattr(raft, "majority", lambda n: 1)
    with pytest.raises(Violation, match="two heads in term"):
        run(False, range(200))
    with pytest.raises(Violation):
        run(True, range(200))
