"""The catalogue's word on the apps here, kept on /data as apps.json: what is served, to whom, and what a reviewer
may open; the artifacts.json a 0.2.0 left is taken over once."""

from __future__ import annotations

from rrotord.catalogue import Catalogue
from rrotord.store import read_json, write_json


def test_the_welcomes_list_and_each_change_are_kept_and_read_back(tmp_path):
    a = Catalogue(tmp_path)
    assert a.get("board") is None and a.state("board", 1) == "unknown" and not a.approved("board", 1)
    a.set_all([{"program": "board", "version": 2, "state": "approved", "roles": ["SRE"]}], ["Platform-Admins"])
    assert a.get("board") == {"version": 2, "state": "approved", "roles": ["sre"]}
    assert a.reviewers == ["platform-admins"] and a.is_reviewer({"Platform-Admins"}) and a.is_reviewer({"ADMIN"})
    assert not a.is_reviewer({"member", "sre"})
    assert a.approved("board", 2) and not a.approved("board", 3) and not a.approved("board", 1)
    assert (a.state("board", 2), a.state("board", 3), a.state("board", 1)) == ("approved", "draft", "retired")
    a.set_one("board", 3, "rejected", [])
    assert Catalogue(tmp_path).get("board") == {"version": 3, "state": "rejected", "roles": []}
    assert Catalogue(tmp_path).reviewers == ["platform-admins"]  # kept with the apps
    assert (tmp_path / "apps.json").is_file() and not (tmp_path / "artifacts.json").exists()
    a.set_all([], None)  # the catalogue holds nothing of this rrotor now; the reviewers stay as they were
    assert a.entries == {} and a.reviewers == ["platform-admins"]


def test_with_the_link_off_every_version_is_approved(tmp_path):
    a = Catalogue(tmp_path, linked=False)
    assert a.approved("board", 7) and a.state("board", 7) == "unknown"


def test_what_a_0_2_0_left_as_artifacts_json_is_taken_over_once(tmp_path):
    old = {"apps": {"board": {"version": 2, "state": "approved", "roles": ["sre"]}}, "reviewers": ["ops"]}
    write_json(tmp_path / "artifacts.json", old)
    a = Catalogue(tmp_path)
    assert a.get("board") == {"version": 2, "state": "approved", "roles": ["sre"]} and a.reviewers == ["ops"]
    assert read_json(tmp_path / "apps.json") == old  # written as apps.json; the old file is left alone
    a.set_one("board", 3, "draft", [])
    assert read_json(tmp_path / "artifacts.json") == old  # never written again
    assert Catalogue(tmp_path).get("board") == {"version": 3, "state": "draft", "roles": []}  # apps.json is read
    assert Catalogue(tmp_path / "empty").entries == {}  # neither file: nothing kept yet
