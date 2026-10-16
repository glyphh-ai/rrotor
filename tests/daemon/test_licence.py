"""The plan's licence: handed in signed, kept on /data, read back at start; what it allows and when it expires."""

from __future__ import annotations

import json

import pytest

from rrotord.errors import RrotorError
from rrotord.licence import Licence
from tests.fakes import keys


def test_a_signed_licence_is_kept_read_back_and_names_its_mods(tmp_path):
    lic = Licence(tmp_path, keys().public)
    assert lic.view() is None and lic.mods() == [] and lic.expired
    given = keys().licence(["ada", "zeta"])
    lic.set(given["payload"], given["signature"])
    assert lic.mods() == ["ada", "zeta"] and not lic.expired
    assert lic.view() == {
        "plan": "Pro",
        "mods": ["ada", "zeta"],
        "expires_at": "2999-01-01T00:00:00Z",
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
    assert lic.expired and lic.mods() == [] and lic.view()["expired"] is True
    with pytest.raises(RrotorError, match="expired"):
        lic.require("ada")
    now = keys().licence(["zeta"])
    lic.set(now["payload"], now["signature"])
    with pytest.raises(RrotorError, match="not in the plan"):
        lic.require("ada")
    odd = keys().licence([], expires="whenever")
    lic.set(odd["payload"], odd["signature"])
    assert lic.expired
    noneish = {"plan": "Pro", "mods": ["ada"], "expires_at": "2999-01-01T00:00:00Z"}
    lic.set(noneish, keys().sign(__import__("rrotord.keys", fromlist=["canonical"]).canonical(noneish)))
    assert lic.mods() == []


def test_a_kept_licence_is_not_read_without_a_key_or_with_a_signature_that_is_not_base64(tmp_path):
    given = keys().licence(["ada"])
    (tmp_path / "licence.json").write_text(json.dumps(given))
    assert Licence(tmp_path, None).payload is None
    (tmp_path / "licence.json").write_text(json.dumps({**given, "signature": "***"}))
    assert Licence(tmp_path, keys().public).payload is None
