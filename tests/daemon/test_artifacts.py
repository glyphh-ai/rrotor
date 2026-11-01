"""The catalogue's word on the apps here, kept on /data: what is served, to whom, and what a reviewer may open."""

from __future__ import annotations

from rrotord.artifacts import Artifacts


def test_the_welcomes_list_and_each_change_are_kept_and_read_back(tmp_path):
    a = Artifacts(tmp_path)
    assert a.get("board") is None and a.state("board", 1) == "unknown" and not a.approved("board", 1)
    a.set_all([{"program": "board", "version": 2, "state": "approved", "roles": ["SRE"]}], ["Platform-Admins"])
    assert a.get("board") == {"version": 2, "state": "approved", "roles": ["sre"]}
    assert a.reviewers == ["platform-admins"] and a.is_reviewer({"Platform-Admins"}) and a.is_reviewer({"ADMIN"})
    assert not a.is_reviewer({"member", "sre"})
    assert a.approved("board", 2) and not a.approved("board", 3) and not a.approved("board", 1)
    assert (a.state("board", 2), a.state("board", 3), a.state("board", 1)) == ("approved", "draft", "retired")
    a.set_one("board", 3, "rejected", [])
    assert Artifacts(tmp_path).get("board") == {"version": 3, "state": "rejected", "roles": []}
    assert Artifacts(tmp_path).reviewers == ["platform-admins"]  # kept with the apps
    a.set_all([], None)  # the catalogue holds nothing of this rrotor now; the reviewers stay as they were
    assert a.entries == {} and a.reviewers == ["platform-admins"]


def test_with_the_link_off_every_version_is_approved(tmp_path):
    a = Artifacts(tmp_path, linked=False)
    assert a.approved("board", 7) and a.state("board", 7) == "unknown"
