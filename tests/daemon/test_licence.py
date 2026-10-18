"""The plan's licence: handed in signed (or as an offline token), kept on /data, read back at start; what it allows,
when it is live, in grace or expired, and which of two is kept."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from rrotord.errors import RrotorError
from rrotord.keys import canonical
from rrotord.licence import Licence, live_until
from tests.fakes import keys

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
RUNTIME = "6f1c2a9e-0000-4000-8000-000000000001"
A_LIST = "WzEsIDJd"  # base64 of [1, 2]: JSON, but not {payload, signature}


def at(days: float) -> str:
    return (NOW + timedelta(days=days)).isoformat().replace("+00:00", "Z")


def licence(tmp_path, runtime_id: str | None = None) -> Licence:
    return Licence(tmp_path, keys().public, runtime_id, clock=lambda: NOW)


def test_a_signed_licence_is_kept_read_back_and_names_its_mods(tmp_path):
    lic = Licence(tmp_path, keys().public)
    assert lic.view() is None and lic.mods() == [] and lic.expired and lic.state is None and lic.licence_id is None
    given = keys().licence(["ada", "zeta"])
    assert lic.set(given["payload"], given["signature"])
    assert lic.mods() == ["ada", "zeta"] and not lic.expired
    view = lic.view()
    assert view is not None and view["days_left"] > 300_000
    assert view == {
        "plan": "Pro",
        "mods": ["ada", "zeta"],
        "features": {},
        "kind": None,
        "licence_id": None,
        "expires_at": "2999-01-01T00:00:00Z",
        "grace_days": 0,
        "live_until": "2999-01-01T00:00:00Z",
        "state": "live",
        "days_left": view["days_left"],
        "expired": False,
    }
    again = Licence(tmp_path, keys().public)
    assert again.mods() == ["ada", "zeta"]
    again.require("ada")


def test_a_tampered_or_malformed_licence_is_refused_and_a_kept_one_that_no_longer_checks_is_dropped(tmp_path):
    lic = Licence(tmp_path, keys().public)
    given = keys().licence(["ada"])
    for payload, signature, code in (
        ({**given["payload"], "plan": "Team"}, given["signature"], "E_LICENCE"),
        (given["payload"], "nope", "E_LICENCE"),
        ("text", given["signature"], "E_LICENCE"),
        (given["payload"], 5, "E_LICENCE"),
    ):
        with pytest.raises(RrotorError) as caught:
            lic.set(payload, signature)
        assert caught.value.code == code
    (tmp_path / "licence.json").write_text(json.dumps({"payload": {"mods": {"ada": {}}}, "signature": "x"}))
    assert Licence(tmp_path, keys().public).payload is None
    (tmp_path / "licence.json").write_text("[]")
    assert Licence(tmp_path, keys().public).payload is None


def test_without_a_licence_key_nothing_is_taken(tmp_path):
    lic = Licence(tmp_path, None)
    given = keys().licence(["ada"])
    with pytest.raises(RrotorError) as caught:
        lic.set(given["payload"], given["signature"])
    assert caught.value.code == "E_CONFIG"


def test_require_says_why_no_licence_expired_or_not_in_the_plan(tmp_path):
    lic = Licence(tmp_path, keys().public)
    with pytest.raises(RrotorError, match="no licence") as caught:
        lic.require("ada")
    assert (caught.value.code, caught.value.status) == ("E_NOT_IN_PLAN", 403)
    old = keys().licence(["ada"], expires="2000-01-01T00:00:00Z")
    lic.set(old["payload"], old["signature"])
    view = lic.view()
    assert lic.expired and lic.mods() == [] and view is not None and view["expired"] is True
    assert (view["state"], view["days_left"]) == ("expired", 0)
    with pytest.raises(RrotorError, match="the licence has expired"):
        lic.require("ada")
    now = keys().licence(["zeta"])
    assert lic.set(now["payload"], now["signature"])
    with pytest.raises(RrotorError, match="not in the plan"):
        lic.require("ada")
    noneish = {"plan": "Pro", "mods": ["ada"], "expires_at": "2999-01-01T00:00:00Z"}
    assert lic.set(noneish, keys().sign(canonical(noneish)))
    assert lic.mods() == []


def test_the_plans_signed_features_are_shown_as_flags_and_none_once_expired(tmp_path):
    lic = licence(tmp_path)
    assert lic.features() == {}
    features = {"sync": True, "ha": False, "offline": False, "sso": False, "seats": 3}
    given = keys().licence(["ada"], expires=at(1), features=features)
    assert lic.set(given["payload"], given["signature"])
    flags = {"ha": False, "offline": False, "sso": False, "sync": True}
    assert lic.features() == flags and lic.view()["features"] == flags  # type: ignore[index]
    lic.clock = lambda: NOW + timedelta(days=2)
    assert lic.expired and lic.features() == {}
    unread = licence(tmp_path / "unread")
    odd = keys().licence(["ada"], features=["sync"])
    assert unread.set(odd["payload"], odd["signature"]) and unread.features() == {}


def test_a_licence_runs_on_through_its_grace_and_then_refuses_only_the_licensed_mods(tmp_path):
    lic = licence(tmp_path)
    given = keys().licence(["ada"], expires=at(-2), grace_days=14, kind="heartbeat", licence_id="l-1")
    assert lic.set(given["payload"], given["signature"])
    view = lic.view()
    assert view is not None and (view["state"], view["days_left"], view["grace_days"]) == ("grace", 12, 14)
    assert (view["kind"], view["licence_id"], view["live_until"]) == ("heartbeat", "l-1", at(12))
    assert lic.mods() == ["ada"] and not lic.expired
    lic.require("ada")
    lic.clock = lambda: NOW + timedelta(days=12, seconds=1)
    assert lic.state == "expired" and lic.mods() == []
    with pytest.raises(RrotorError, match="the licence has expired") as caught:
        lic.require("ada")
    assert caught.value.code == "E_NOT_IN_PLAN"
    lic.clock = lambda: NOW - timedelta(days=3)
    assert lic.state == "live"


def test_of_two_valid_licences_the_one_live_longest_is_kept(tmp_path):
    lic = licence(tmp_path)
    offline = keys().licence(["ada"], expires=at(365), grace_days=14, kind="offline")
    link = keys().licence(["ada", "zeta"], expires=at(1), grace_days=14, kind="link")
    assert lic.set(offline["payload"], offline["signature"])
    assert not lic.set(link["payload"], link["signature"])  # live for less: the offline one stays
    assert lic.view()["kind"] == "offline"  # type: ignore[index]
    renewed = keys().licence(["ada"], expires=at(365), grace_days=14, kind="offline", licence_id="l-2")
    assert lic.set(renewed["payload"], renewed["signature"]) and lic.licence_id == "l-2"  # as long: the newer
    unreadable = keys().licence(["ada"], expires="whenever")
    assert not lic.set(unreadable["payload"], unreadable["signature"])
    fresh = licence(tmp_path / "fresh")
    assert fresh.set(unreadable["payload"], unreadable["signature"]) and fresh.state == "expired"
    assert fresh.set(link["payload"], link["signature"]) and fresh.state == "live"


@pytest.mark.parametrize(
    "fields",
    [{"grace_days": -1}, {"grace_days": True}, {"grace_days": "14"}, {"expires_at": "2999-01-01T00:00:00"}],
)
def test_a_licence_whose_dates_do_not_read_has_expired(tmp_path, fields):
    payload = keys().licence(["ada"])["payload"] | fields
    assert live_until(payload) is None and live_until(None) is None
    lic = licence(tmp_path)
    assert lic.set(payload, keys().sign(canonical(payload)))
    assert lic.state == "expired" and lic.view()["days_left"] == 0  # type: ignore[index]


def test_a_licence_for_another_runtime_is_refused(tmp_path):
    lic = licence(tmp_path, RUNTIME)
    other = keys().licence(["ada"], runtime_id="6f1c2a9e-0000-4000-8000-000000000002")
    with pytest.raises(RrotorError, match="another runtime") as caught:
        lic.set(other["payload"], other["signature"])
    assert caught.value.code == "E_LICENCE"
    own = keys().licence(["ada"], runtime_id=RUNTIME)
    assert lic.set(own["payload"], own["signature"])
    assert licence(tmp_path / "local").set(other["payload"], other["signature"])  # no runtime id: not checked


def test_an_offline_token_is_taken_as_the_token_as_token_or_as_the_json(tmp_path):
    given = keys().licence(["ada"], expires=at(365), grace_days=14, kind="offline")
    token = keys().token(given)
    assert licence(tmp_path / "a").take(token)
    assert licence(tmp_path / "b").take({"token": token})
    assert licence(tmp_path / "c").take(given)
    for bad in ("***", A_LIST, 5):
        with pytest.raises(RrotorError) as caught:
            licence(tmp_path / "d").take(bad)
        assert caught.value.code == "E_LICENCE"


def test_offline_licences_are_read_at_boot_and_a_bad_or_expired_one_never_stops_it(tmp_path, caplog):
    lic = licence(tmp_path)
    lic.load(None)  # neither the variable nor the file: nothing
    assert lic.payload is None
    (tmp_path / "licence.token").write_text("\n")
    lic.load(None)
    assert lic.payload is None
    short = keys().licence(["ada"], expires=at(30), kind="offline")
    long = keys().licence(["ada"], expires=at(300), kind="offline", licence_id="from-file")
    (tmp_path / "licence.token").write_text(keys().token(long) + "\n")
    with caplog.at_level(logging.INFO, logger="rrotor.licence"):
        lic.load(keys().token(short))
    assert lic.licence_id == "from-file" and "offline licence read" in caplog.text
    expired = licence(tmp_path / "expired")
    with caplog.at_level(logging.WARNING, logger="rrotor.licence"):
        expired.load(keys().token(keys().licence(["ada"], expires=at(-30), kind="offline")))
        expired.load("not a token")
    assert expired.state == "expired" and "an offline licence was refused" in caplog.text


def test_a_kept_licence_is_not_read_without_a_key_or_with_a_signature_that_is_not_base64(tmp_path):
    given = keys().licence(["ada"])
    (tmp_path / "licence.json").write_text(json.dumps(given))
    assert Licence(tmp_path, None).payload is None
    (tmp_path / "licence.json").write_text(json.dumps({**given, "signature": "***"}))
    assert Licence(tmp_path, keys().public).payload is None
