"""The licence key's format: `<base64url payload>.<base64url signature>`, unpadded, Ed25519 over the payload's exact
bytes with a public key in the code; every known field there with its type; and release N's legacy licence mapped."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from rrotord import config, licence_key
from rrotord.daemon import Daemon
from rrotord.keys import canonical
from rrotord.licence_key import COMMUNITY, GRACE_DAYS, Plan, Refused, b64url, legacy, parse, unpack
from tests.fakes import Keys, LocalLauncher, keys, ready
from tests.fakes import b64url as encode

RUNTIME = "6f1c2a9e-0000-4000-8000-000000000001"


def other() -> Keys:
    private = Ed25519PrivateKey.generate()
    return Keys(private, private.public_key())


def reason(text: str) -> str:
    with pytest.raises(Refused) as caught:
        parse(text)
    return caught.value.reason


def test_glyphhs_production_key_is_the_codes_and_raw_32_bytes():
    assert licence_key.GLYPHH == "vDD0qLKsfgWsqbhH9tMnht4egmnlph72QzVvvpzjvSs"
    assert base64.b64encode(b64url(licence_key.GLYPHH)).decode() == "vDD0qLKsfgWsqbhH9tMnht4egmnlph72QzVvvpzjvSs="
    assert licence_key.GRACE_DAYS == GRACE_DAYS == 14


def test_a_key_signed_by_a_key_in_the_tuple_parses_with_its_own_numbers():
    text = keys().key(tier="team", max_nodes=10, max_precedents=None, cluster_sync_enabled=False, extra={"x": 1})
    key = parse(text + "\n")
    assert key.plan == Plan("team", 10, None, False) and key.raw == text and not key.legacy
    assert key.issued_at == datetime(2026, 10, 1, tzinfo=UTC) and key.expires_at.year == 2999
    assert key.customer_id == "cus_1" and len(key.license_id or "") == 36
    assert parse(keys().key(max_nodes=None, max_precedents=0)).plan.max_precedents == 0


def test_a_rotation_takes_either_key_in_the_tuple(monkeypatch):
    nxt = other()
    monkeypatch.setattr(licence_key, "PUBLIC_KEYS", (nxt.public_url, keys().public_url))
    assert parse(keys().key()).plan.tier == "pro" and parse(nxt.key()).plan.tier == "pro"


def test_one_byte_changed_in_the_payload_or_the_signature_is_bad():
    payload, signature = keys().key().split(".")
    raw = bytearray(b64url(payload))
    raw[2] ^= 1
    assert reason(f"{encode(bytes(raw))}.{signature}") == "the signature does not verify"
    sig = bytearray(b64url(signature))
    sig[0] ^= 1
    assert reason(f"{payload}.{encode(bytes(sig))}") == "the signature does not verify"
    assert reason(other().key()) == "the signature does not verify"


def test_padding_standard_base64_and_other_shapes_are_bad():
    payload, signature = keys().key().split(".")
    raw = b64url(payload)
    assert reason(f"{payload}==.{signature}") == "not base64url without padding"
    assert reason(f"{payload}.{signature}==") == "not base64url without padding"
    standard = base64.b64encode(b"\xfb\xff" + raw).decode().rstrip("=")
    assert "+" in standard or "/" in standard
    assert reason(f"{standard}.{signature}") == "not base64url without padding"
    assert reason(f"{payload}.{signature}.x") == "not <payload>.<signature>"
    assert reason("no-dot-at-all") == "not <payload>.<signature>"
    assert reason(f"AAAAA.{signature}") == "not base64url without padding"  # a length base64 cannot have
    assert reason(f"AB.{signature}") == "not base64url without padding"  # stray bits in the last character
    assert reason(f".{signature}") == "not base64url without padding"


def signed(payload: bytes) -> str:
    return f"{encode(payload)}.{encode(keys().private.sign(payload))}"


def test_a_payload_that_is_not_a_utf8_json_object_is_bad():
    assert reason(signed(b"\xff\xfe")) == "the payload is not UTF-8 JSON"
    assert reason(signed(b"{not json")) == "the payload is not UTF-8 JSON"
    assert reason(signed(b"[1, 2]")) == "the payload is not a JSON object"


@pytest.mark.parametrize(
    ("fields", "why"),
    [
        ({"license_id": "nope"}, "license_id is not a UUID"),
        ({"license_id": 7}, "license_id is not a UUID"),
        ({"tier": "gold"}, "tier is not one of free, pro, team, enterprise"),
        ({"max_nodes": 0}, "max_nodes is not a whole number of at least 1, or null"),
        ({"max_nodes": True}, "max_nodes is not a whole number of at least 1, or null"),
        ({"max_precedents": -1}, "max_precedents is not a whole number of at least 0, or null"),
        ({"max_precedents": "5000"}, "max_precedents is not a whole number of at least 0, or null"),
        ({"cluster_sync_enabled": 1}, "cluster_sync_enabled is not true or false"),
        ({"customer_id": None}, "customer_id is not a string"),
        ({"issued_at": "2026-10-01"}, "issued_at is not an RFC 3339 UTC time"),
        ({"issued_at": "2026-10-01T00:00:00+02:00"}, "issued_at is not an RFC 3339 UTC time"),
        ({"expires_at": "soon"}, "expires_at is not an RFC 3339 UTC time"),
        ({"expires_at": 1767225600}, "expires_at is not an RFC 3339 UTC time"),
    ],
)
def test_a_mistyped_field_is_bad(fields, why):
    assert reason(keys().key(**fields)) == why


def test_a_missing_field_is_bad_and_names_it():
    assert reason(keys().key(drop=("tier",))) == "tier missing"
    assert reason(keys().key(drop=("max_nodes", "customer_id"))) == "max_nodes, customer_id missing"


def test_rrotor_licence_public_key_is_not_trusted(tmp_path, short_dir):
    attacker = other()
    env = {
        "RROTOR_MODE": "self",
        "RROTOR_RUNTIME_ID": RUNTIME,
        "RROTOR_RUNTIME_TOKEN": "tok_" + "c" * 20,
        "RROTOR_PUBLIC_HOST": "rrotor.acme.com",
        "RROTOR_LINK": "off",
        "RROTOR_DATA": str(tmp_path / "data"),
        "RROTOR_RUN_DIR": str(short_dir),
        "RROTOR_LICENCE_PUBLIC_KEY": attacker.public_text,
        "GLYPHH_LICENSE_KEY": attacker.key(tier="enterprise", max_nodes=None, max_precedents=None),
    }
    d = Daemon(config.load(env), ready(), launcher=LocalLauncher())
    assert d.licence.state == "bad" and d.licence.community and d.licence.limits() == COMMUNITY


def legacy_licence(**fields: Any) -> tuple[dict[str, Any], str]:
    payload = {"plan": "Team", "mods": {"ada": {"limits": {}}}, "expires_at": "2026-10-20T00:00:00Z"} | fields
    return payload, keys().sign(canonical(payload))


def test_a_legacy_licence_is_mapped_in_release_n():
    payload, signature = legacy_licence(
        grace_days=14, features={"sync": True}, licence_id="l-1", org_id="o-1", runtime_id=RUNTIME
    )
    key = legacy(payload, signature, RUNTIME)
    assert key.legacy and key.raw == {"payload": payload, "signature": signature} and key.issued_at is None
    assert key.plan == Plan("team", 10, 500000, True) and (key.license_id, key.customer_id) == ("l-1", "o-1")
    assert key.expires_at == datetime(2026, 10, 20, tzinfo=UTC) + timedelta(days=14)  # expires_at plus grace_days
    payload, signature = legacy_licence(plan="Hobby")
    plain = legacy(payload, signature, None)  # an unknown plan is free; no grace_days, no sync, no ids
    assert plain.plan == COMMUNITY and plain.expires_at == datetime(2026, 10, 20, tzinfo=UTC)
    assert (plain.license_id, plain.customer_id) == (None, None)
    unsynced = legacy(*legacy_licence(plan="enterprise", features=["sync"]), None)
    assert unsynced.plan == Plan("enterprise", None, None, False)


def test_a_legacy_licence_that_does_not_verify_names_another_runtime_or_has_no_dates_is_refused():
    payload, signature = legacy_licence()
    cases: list[tuple[tuple[Any, Any], str]] = [
        (({**payload, "plan": "Enterprise"}, signature), "the signature does not verify"),
        ((payload, "***"), "the signature does not verify"),
        ((payload, other().sign(canonical(payload))), "the signature does not verify"),
        (("text", signature), "not {payload, signature}"),
        ((payload, 5), "not {payload, signature}"),
        (legacy_licence(runtime_id="6f1c2a9e-0000-4000-8000-000000000002"), "the licence is for another runtime"),
        (legacy_licence(expires_at="2026-10-20T00:00:00"), "its expires_at or grace_days do not read"),
        (legacy_licence(grace_days=-1), "its expires_at or grace_days do not read"),
        (legacy_licence(grace_days=True), "its expires_at or grace_days do not read"),
    ]
    for (sent, signed), why in cases:
        with pytest.raises(Refused) as caught:
            legacy(sent, signed, RUNTIME)
        assert caught.value.reason == why


def test_a_legacy_token_unpacks_padded_or_not_and_junk_is_refused():
    payload, signature = legacy_licence()
    token = base64.urlsafe_b64encode(json.dumps({"payload": payload, "signature": signature}).encode()).decode()
    assert unpack(token) == unpack(token.rstrip("=")) == (payload, signature)
    for junk in ("***", "WzEsIDJd"):  # not base64; base64 of [1, 2]
        with pytest.raises(Refused, match="neither a key"):
            unpack(junk)


def test_a_keys_state_runs_live_then_grace_then_expired():
    key = parse(keys().key(expires_at="2026-10-20T00:00:00Z"))
    end = datetime(2026, 10, 20, tzinfo=UTC)
    assert key.state(end - timedelta(seconds=1)) == "live"
    assert key.state(end) == "grace" and key.state(end + timedelta(days=14, seconds=-1)) == "grace"
    assert key.state(end + timedelta(days=14)) == "expired"
