"""The access store: the runtime keys' hashes and the pause, kept on /data, compared in constant time."""

from __future__ import annotations

from rrotord.access import Access, digest
from tests.fakes import token


def test_keys_seed_from_the_environment_then_the_cloud_sets_the_whole_list(tmp_path):
    first, second = token(), token()
    access = Access(tmp_path, (digest(first), "not a hash"))
    assert access.keys == [digest(first)] and access.allows(first) and not access.allows(second)
    assert not access.allows("")
    again = Access(tmp_path, ())  # read back: the saved list wins over an empty environment
    assert again.keys == [digest(first)]
    again.set_keys([digest(second), "junk"])
    assert Access(tmp_path, (digest(first),)).keys == [digest(second)]  # saved beats the environment
    assert again.allows(second) and not again.allows(first)
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
