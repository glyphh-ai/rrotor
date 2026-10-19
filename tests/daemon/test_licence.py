"""The licence key held: Community with none, a bad one logged and never fatal, a good one's limits, grace, Community
again past it; which of several is held; the files read again; the key kept across a restart; release N's legacy
licence taken, and a key preferred to it."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from rrotord import licence_key
from rrotord.errors import RrotorError
from rrotord.keys import canonical
from rrotord.licence import Licence
from rrotord.licence_key import COMMUNITY, Plan
from tests.daemon.test_licence_key import other
from tests.fakes import keys

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
RUNTIME = "6f1c2a9e-0000-4000-8000-000000000001"


def at(days: float) -> str:
    return (NOW + timedelta(days=days)).isoformat().replace("+00:00", "Z")


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def licence(tmp_path, clock=None, runtime_id: str | None = None) -> Licence:
    return Licence(tmp_path / "data", runtime_id, clock=clock or (lambda: NOW), etc=tmp_path / "etc" / "license.key")


def legacy_token(**fields: Any) -> tuple[dict[str, Any], str]:
    payload = {"plan": "Team", "mods": {}, "expires_at": at(1), "grace_days": 14} | fields
    signed = {"payload": payload, "signature": keys().sign(canonical(payload))}
    return signed, keys().token(signed)


def test_no_key_anywhere_is_community_said_once(tmp_path, caplog):
    lic = licence(tmp_path)
    with caplog.at_level(logging.INFO, logger="rrotor.licence"):
        lic.start([])
    assert caplog.text.count("community: no licence key") == 1
    assert (lic.state, lic.community, lic.limits(), lic.licence_id, lic.days_left()) == (
        "none",
        True,
        COMMUNITY,
        None,
        None,
    )
    assert lic.view() == {
        "tier": "free",
        "community": True,
        "license_id": None,
        "customer_id": None,
        "source": None,
        "issued_at": None,
        "expires_at": None,
        "state": "none",
        "days_left": None,
        "max_nodes": 1,
        "max_precedents": 5000,
        "cluster_sync_enabled": False,
        "precedents": {"held": None, "cap": 5000, "over": False},
    }
    assert not (tmp_path / "data" / "licence.json").exists()
    assert lic.view(5000)["precedents"] == {"held": 5000, "cap": 5000, "over": False}  # at the cap, not over
    assert lic.view(5001)["precedents"] == {"held": 5001, "cap": 5000, "over": True}
    assert lic.plan_name == "Community"


def test_a_bad_key_is_logged_at_error_with_its_source_and_the_node_runs_as_community(tmp_path, caplog):
    lic = licence(tmp_path)
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "license.key").write_text(other().key() + "\n")
    with caplog.at_level(logging.INFO, logger="rrotor.licence"):
        lic.start([("GLYPHH_LICENSE_KEY", keys().key(drop=("tier",)))])
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [(r.getMessage(), r.source) for r in errors] == [
        ("licence key refused: tier missing", "GLYPHH_LICENSE_KEY"),
        ("licence key refused: the signature does not verify", str(tmp_path / "etc" / "license.key")),
    ]
    assert "community: no licence key" not in caplog.text
    assert (lic.state, lic.community, lic.limits()) == ("bad", True, COMMUNITY)
    assert lic.view()["state"] == "bad" and lic.view()["tier"] == "free"
    caplog.clear()
    lic.tick()  # every hourly re-read logs it again
    assert "licence key refused: the signature does not verify" in caplog.text
    (tmp_path / "etc" / "license.key").write_text(keys().key(tier="team", max_nodes=10, max_precedents=500000))
    lic.tick()
    assert lic.state == "live" and lic.limits().tier == "team" and lic.refused == {"GLYPHH_LICENSE_KEY": "tier missing"}


def test_a_good_key_sets_its_own_limits_and_shows_as_the_spec_says(tmp_path):
    text = keys().key(license_id="2c7f0d9e-0000-4000-8000-000000000001", issued_at=at(-18), expires_at=at(12))
    lic = licence(tmp_path)
    lic.start([("GLYPHH_LICENSE_KEY", text)])
    assert lic.limits() == Plan("pro", 3, 50000, True) and not lic.community
    assert lic.view() == {
        "tier": "pro",
        "community": False,
        "license_id": "2c7f0d9e-0000-4000-8000-000000000001",
        "customer_id": "cus_1",
        "source": "GLYPHH_LICENSE_KEY",
        "issued_at": at(-18),
        "expires_at": at(12),
        "state": "live",
        "days_left": 12,
        "max_nodes": 3,
        "max_precedents": 50000,
        "cluster_sync_enabled": True,
        "precedents": {"held": None, "cap": 50000, "over": False},
    }
    assert lic.plan_name == "Pro"
    assert json.loads((tmp_path / "data" / "licence.json").read_text()) == {"key": text, "source": "GLYPHH_LICENSE_KEY"}
    enterprise = keys().key(tier="enterprise", max_nodes=None, max_precedents=None, issued_at=at(-1))
    lic.offer(enterprise, "heartbeat")
    assert lic.view(10**9)["precedents"] == {"held": 10**9, "cap": None, "over": False}
    assert lic.plan_name == "Enterprise"


def test_an_expired_key_holds_its_limits_through_14_days_of_grace_warned_daily_then_community(tmp_path, caplog):
    clock = Clock()
    lic = licence(tmp_path, clock)
    with caplog.at_level(logging.WARNING, logger="rrotor.licence"):
        lic.start([("RROTOR_LICENCE", keys().key(issued_at=at(-40), expires_at=at(-2)))])
        lic.tick()  # the same day: said once
    assert caplog.text.count("licence key expired 2026-10-17; Community limits from 2026-10-31") == 1
    assert (lic.state, lic.days_left(), lic.limits().tier, lic.community) == ("grace", 12, "pro", False)
    assert lic.view()["state"] == "grace" and lic.view()["max_precedents"] == 50000
    clock.now = NOW + timedelta(days=1)
    with caplog.at_level(logging.WARNING, logger="rrotor.licence"):
        lic.tick()
    assert caplog.text.count("Community limits from 2026-10-31") == 2  # the next day, again
    clock.now = NOW + timedelta(days=12)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="rrotor.licence"):
        lic.tick()
    assert "licence key expired 2026-10-17; Community limits since 2026-10-31" in caplog.text
    view = lic.view()
    assert (lic.state, lic.community, lic.limits(), lic.days_left()) == ("expired", True, COMMUNITY, 0)
    assert (view["tier"], view["state"], view["max_precedents"], view["expires_at"]) == (
        "pro",
        "expired",
        5000,
        at(-2),
    )  # what expired is still shown; Community's limits apply


def test_never_an_exception_at_start_whatever_the_files_hold(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "licence.json").write_text("{not json")
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "license.key").write_bytes(b"\xff\xfe")
    (tmp_path / "data" / "licence.token").mkdir()  # not a file
    lic = licence(tmp_path)
    lic.start([("RROTOR_LICENCE", "[1, 2]"), ("GLYPHH_LICENSE_KEY", "***")])
    assert lic.state == "bad" and lic.limits() == COMMUNITY
    (tmp_path / "data" / "licence.json").write_text("[]")
    assert licence(tmp_path).state == "none"
    (tmp_path / "data" / "licence.json").write_text('{"source": "x"}')
    assert licence(tmp_path).state == "none"


def test_of_the_keys_found_the_newest_issued_is_held_not_the_longest_or_the_highest(tmp_path):
    lic = licence(tmp_path)
    (tmp_path / "etc").mkdir()
    old_team = keys().key(tier="team", max_nodes=10, max_precedents=500000, issued_at=at(-60), expires_at=at(300))
    (tmp_path / "etc" / "license.key").write_text(old_team)
    lic.start([("GLYPHH_LICENSE_KEY", keys().key(issued_at=at(-30), expires_at=at(1)))])
    assert lic.held_from is not None and lic.held_from[0] == "GLYPHH_LICENSE_KEY"
    assert lic.limits().tier == "pro"  # the older Team key in the file does not override a newer Pro one
    free = keys().key(
        tier="free", max_nodes=1, max_precedents=5000, cluster_sync_enabled=False, issued_at=at(-1), expires_at=at(30)
    )
    assert lic.offer(free, "heartbeat") is lic.held  # a downgrade at the next payment takes effect
    assert lic.limits().tier == "free" and not lic.community
    tie = keys().key(issued_at=at(-1), expires_at=at(31))
    assert lic.offer(tie, "welcome") is lic.held  # issued together: the later expiry
    held = lic.held
    older = keys().key(tier="enterprise", max_nodes=None, max_precedents=None, issued_at=at(-5), expires_at=at(5000))
    assert lic.offer(older, "welcome") is not None and lic.held is held  # an older one from the same source: kept out
    assert lic.offer("junk.junk", "welcome") is None and lic.held is held  # a bad one: no change


def test_a_newer_key_past_grace_loses_to_an_older_one_within_it_and_with_none_within_the_newest_is_held(tmp_path):
    lic = licence(tmp_path)
    lic.offer(keys().key(tier="team", max_nodes=10, issued_at=at(-200), expires_at=at(100)), "file")
    lic.offer(keys().key(issued_at=at(-60), expires_at=at(-30)), "heartbeat")
    assert lic.held is not None and lic.held.plan.tier == "team" and lic.state == "live"
    gone = licence(tmp_path / "gone")
    gone.offer(keys().key(tier="team", max_nodes=10, issued_at=at(-200), expires_at=at(-100)), "file")
    gone.offer(keys().key(issued_at=at(-60), expires_at=at(-30)), "heartbeat")
    assert gone.held is not None and gone.held.plan.tier == "pro"
    assert (gone.state, gone.limits()) == ("expired", COMMUNITY)


def test_the_key_held_is_kept_on_data_and_survives_a_restart_without_the_environment(tmp_path):
    text = keys().key(tier="team", max_nodes=10)
    licence(tmp_path).start([("GLYPHH_LICENSE_KEY", text)])
    again = licence(tmp_path)
    again.start([])
    assert again.held is not None and again.held.raw == text and again.view()["source"] == "GLYPHH_LICENSE_KEY"


def test_a_kept_key_that_no_longer_verifies_is_refused_and_logged(tmp_path, caplog, monkeypatch):
    licence(tmp_path).offer(keys().key(), "heartbeat")
    monkeypatch.setattr(licence_key, "PUBLIC_KEYS", (other().public_url,))  # the key rotated away
    with caplog.at_level(logging.ERROR, logger="rrotor.licence"):
        lic = licence(tmp_path)
    assert lic.state == "bad" and "the signature does not verify" in caplog.text


def test_the_key_file_is_read_again_and_a_renewal_dropped_in_is_held(tmp_path):
    lic = licence(tmp_path)
    lic.start([])
    assert lic.state == "none"
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "licence.token").write_text(keys().key(issued_at=at(-30), expires_at=at(1)) + "\n")
    lic.reread()
    first = lic.held
    assert first is not None and lic.view()["source"] == str(tmp_path / "data" / "licence.token")
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "license.key").write_text(keys().key(issued_at=at(0), expires_at=at(31)))
    lic.tick()
    assert lic.held is not first and lic.view()["source"] == str(tmp_path / "etc" / "license.key")
    assert lic.view()["expires_at"] == at(31)


def test_a_legacy_licence_is_still_taken_in_release_n_from_every_source(tmp_path):
    signed, token = legacy_token(licence_id="l-env", org_id="o-1", runtime_id=RUNTIME)
    lic = licence(tmp_path, runtime_id=RUNTIME)
    lic.start([("RROTOR_LICENCE", token)])
    assert lic.held is not None and lic.held.legacy and lic.licence_id == "l-env"
    assert lic.limits() == Plan("team", 10, 500000, False) and lic.view()["issued_at"] is None
    saved = json.loads((tmp_path / "data" / "licence.json").read_text())
    assert saved == {"licence": signed, "source": "RROTOR_LICENCE"}
    assert licence(tmp_path, runtime_id=RUNTIME).licence_id == "l-env"  # kept across a restart
    (tmp_path / "etc").mkdir()
    longer, _ = legacy_token(expires_at=at(300), licence_id="l-file")
    (tmp_path / "etc" / "license.key").write_text(json.dumps(longer))  # as its JSON
    lic.reread()
    assert lic.licence_id == "l-file"  # of two legacy licences, the one live longest
    assert lic.offer({"token": keys().token(longer)}, "welcome") is not None
    other_runtime, _ = legacy_token(runtime_id="6f1c2a9e-0000-4000-8000-000000000002")
    assert lic.offer(other_runtime, "heartbeat") is None
    assert lic.refused["heartbeat"] == "the licence is for another runtime"


def test_a_key_is_preferred_to_a_legacy_licence_while_it_counts(tmp_path):
    lic = licence(tmp_path)
    signed, _ = legacy_token(expires_at=at(300), plan="Enterprise")
    lic.offer(signed, "welcome")
    assert lic.held is not None and lic.held.legacy and lic.limits().tier == "enterprise"
    key = keys().key(issued_at=at(-30), expires_at=at(1))
    assert lic.offer(key, "welcome") is lic.held and lic.limits().tier == "pro"
    lic.clock = lambda: NOW + timedelta(days=20)  # the key past grace, the legacy licence still live
    assert lic.held is not None and lic.held.legacy and lic.state == "live"


def test_a_licence_from_before_the_key_kept_on_data_is_read_as_legacy(tmp_path):
    signed, _ = legacy_token(licence_id="l-old")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "licence.json").write_text(json.dumps(signed))
    lic = licence(tmp_path)
    assert lic.licence_id == "l-old" and lic.view()["source"] == "saved"


def test_put_takes_a_key_or_a_legacy_licence_and_refuses_a_bad_one(tmp_path):
    lic = licence(tmp_path)
    assert lic.take(keys().key(issued_at=at(-1)))
    assert not lic.take(keys().key(issued_at=at(-10)))  # older: kept as the api's, not held
    _, token = legacy_token()
    assert not lic.take(token)  # a key is preferred
    assert lic.held is not None and lic.view()["source"] == "api"
    for bad, why in ((5, "neither a key nor a legacy licence"), ("x.y", "not base64url without padding")):
        with pytest.raises(RrotorError) as caught:
            lic.take(bad)
        assert (caught.value.code, caught.value.status) == ("E_LICENCE", 400)
        assert caught.value.message == f"The licence key is refused: {why}."


def test_a_key_taken_past_grace_or_refused_is_told_for_the_audit_log(tmp_path):
    clock = Clock()
    held = licence(tmp_path, clock)
    told: list[tuple[str, dict[str, Any]]] = []
    held.on_event = lambda kind, detail: told.append((kind, detail))
    first = keys().key(issued_at=at(-30), expires_at=at(1))
    held.offer(first, "heartbeat")
    held.offer(first, "welcome")  # the same key again: not a change
    held.offer("junk.junk", "/etc/rrotor/license.key")
    assert [k for k, _ in told] == ["licence.changed", "licence.bad"]
    assert told[0][1]["source"] == "heartbeat" and told[0][1]["tier"] == "pro"
    assert told[1][1] == {"source": "/etc/rrotor/license.key", "reason": told[1][1]["reason"]}
    clock.now += timedelta(days=20)
    held.tick()
    held.tick()  # told once per key
    assert [k for k, _ in told] == ["licence.changed", "licence.bad", "licence.expired"]
    newer = keys().key(issued_at=at(10), expires_at=at(60))
    held.offer(newer, "api")
    assert told[-1][0] == "licence.changed" and told[-1][1]["source"] == "api"
    licence(tmp_path / "quiet").offer("junk.junk", "x")  # no one to tell
