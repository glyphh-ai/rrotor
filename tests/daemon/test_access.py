"""The access store: the runtime keys' hashes and the pause, kept on /data, compared in constant time."""

from __future__ import annotations

import datetime as dt
from typing import Any

from rrotord.access import Access, Usage, digest, utcnow
from tests.fakes import token


def test_keys_seed_from_the_environment_then_the_cloud_sets_the_whole_list(tmp_path):
    first, second = token(), token()
    access = Access(tmp_path, (digest(first), "not a hash"))
    assert access.keys == [digest(first)] and access.match(first) == digest(first) and access.match(second) is None
    assert access.match("") is None
    again = Access(tmp_path, ())  # read back: the saved list wins over an empty environment
    assert again.keys == [digest(first)]
    again.set_keys([digest(second), "junk"])
    assert Access(tmp_path, (digest(first),)).keys == [digest(second)]  # saved beats the environment
    assert again.match(second) == digest(second) and again.match(first) is None
    assert again.view() == {"keys": 1, "paused": None}


def test_a_pause_is_kept_until_resumed(tmp_path):
    access = Access(tmp_path)
    assert access.paused is None and access.keys == []
    access.pause(None)
    assert Access(tmp_path).paused == {"reason": "runtime_hours"}
    access.pause("hours")
    assert access.view()["paused"] == {"reason": "hours"}
    access.resume()
    assert Access(tmp_path).paused is None


def test_a_broken_file_is_ignored(tmp_path):
    (tmp_path / "access.json").write_text("[1, 2]")
    assert Access(tmp_path, ("a" * 64,)).keys == ["a" * 64]


def test_the_key_matched_is_the_one_presented_among_several(tmp_path):
    first, second, third = token(), token(), token()
    access = Access(tmp_path, (digest(first), digest(second), digest(third)))
    assert [access.match(k) for k in (first, second, third)] == [digest(first), digest(second), digest(third)]


def test_usage_counts_calls_per_key_and_starts_again_when_taken():
    times = iter(dt.datetime(2026, 10, 16, 9, 0, s, tzinfo=dt.UTC) for s in range(10))
    usage = Usage(clock=lambda: next(times))
    assert usage.take() == []
    usage.count("a" * 64)
    usage.count("b" * 64)
    usage.count("a" * 64)
    assert usage.take() == [
        {"key_sha256": "a" * 64, "calls": 2, "last_used_at": "2026-10-16T09:00:02+00:00"},
        {"key_sha256": "b" * 64, "calls": 1, "last_used_at": "2026-10-16T09:00:01+00:00"},
    ]
    assert usage.take() == []
    assert utcnow().tzinfo is dt.UTC and Usage().clock is utcnow


def test_a_change_to_the_keys_is_told_with_the_hashes_added_and_removed(tmp_path):
    told: list[dict[str, Any]] = []
    a = Access(tmp_path, ("a" * 64,), on_change=told.append)
    a.set_keys(["a" * 64, "b" * 64])
    a.set_keys(["a" * 64, "b" * 64])  # the same list: nothing told
    a.set_keys(["b" * 64])
    assert told == [
        {"keys": 2, "added": ["b" * 64], "removed": []},
        {"keys": 1, "added": [], "removed": ["a" * 64]},
    ]
    Access(tmp_path / "quiet").set_keys(["c" * 64])  # no one to tell


def test_a_followers_calls_are_added_to_the_heads():
    usage = Usage(clock=lambda: dt.datetime(2026, 10, 19, 9, tzinfo=dt.UTC))
    usage.count("a" * 64)
    usage.merge(
        [
            {"key_sha256": "a" * 64, "calls": 2, "last_used_at": "2026-10-19T10:00:00+00:00"},
            {"key_sha256": "b" * 64, "calls": 1, "last_used_at": "2026-10-19T08:00:00+00:00"},
            {"key_sha256": "c" * 64, "calls": "x", "last_used_at": "2026-10-19T08:00:00+00:00"},
            {"key_sha256": "d" * 64, "calls": 1, "last_used_at": "yesterday"},
        ]
    )
    taken = {t["key_sha256"][0]: (t["calls"], t["last_used_at"]) for t in usage.take()}
    assert taken == {"a": (3, "2026-10-19T10:00:00+00:00"), "b": (1, "2026-10-19T08:00:00+00:00")}
