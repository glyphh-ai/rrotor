"""The replication log: segments of JSON lines numbered by seq, read past a seq, trimmed to what every member applied,
bounded by RROTOR_CLUSTER_LOG_KEEP, reset after a snapshot, and found again after a restart."""

from __future__ import annotations

import pytest

from rrotord.cluster import log as log_module
from rrotord.cluster.log import Log


def test_entries_are_numbered_kept_in_segments_and_read_past_a_seq(tmp_path, monkeypatch):
    monkeypatch.setattr(log_module, "SEGMENT", 100)
    log = Log(tmp_path / "log", 1 << 20)
    assert (log.last, log.first, log.read(0)) == (0, 1, [])
    for i in range(6):
        assert log.append({"kind": "keys", "hashes": [str(i) * 40]})["seq"] == i + 1
    assert len(log.segments()) > 1 and log.size() > 0
    assert [e["seq"] for e in log.read(2)] == [3, 4, 5, 6]
    assert [e["seq"] for e in log.read(3)] == [4, 5, 6]
    assert [e["seq"] for e in log.read(0, limit=2)] == [1, 2]
    assert [e["seq"] for e in log.read(0, max_bytes=10)] == [1]  # always one
    with pytest.raises(ValueError, match="does not follow"):
        log.append({"seq": 9})
    log.append({"seq": 7, "kind": "keys"})  # a follower's, as the head numbered it
    again = Log(tmp_path / "log", 1 << 20)
    assert again.last == 7 and again.first == 1


def test_trimmed_to_what_every_member_applied_and_bounded_but_never_the_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(log_module, "SEGMENT", 50)
    log = Log(tmp_path / "log", 1 << 20)
    for _ in range(5):
        log.append({"kind": "keys", "hashes": ["x" * 40]})
    log.trim(3)
    assert log.first == 4 and log.last == 5
    log.trim(99)
    assert len(log.segments()) == 1 and log.last == 5
    small = Log(tmp_path / "small", 120)
    for _ in range(6):
        small.append({"kind": "keys", "hashes": ["y" * 40]})
    assert small.size() <= 120 + 100 and small.first > 1 and small.last == 6


def test_reset_after_a_snapshot_and_an_empty_segment(tmp_path):
    log = Log(tmp_path / "log", 1 << 20)
    log.append({"kind": "keys"})
    log.reset(40)
    assert (log.last, log.first, log.base) == (40, 41, 40)
    assert Log(tmp_path / "log", 1 << 20).last == 40
    (tmp_path / "log" / log_module.NAME.format(41)).write_text("")
    assert Log(tmp_path / "log", 1 << 20).last == 40
    (tmp_path / "log" / "base").write_text("not a number")
    assert Log(tmp_path / "log", 1 << 20).base == 0
