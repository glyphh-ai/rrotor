"""Public keys from text, signatures checked, and the canonical bytes a licence is signed over."""

from __future__ import annotations

import base64

import pytest

from rrotord.errors import RrotorError
from rrotord.keys import canonical, public_key, unb64, verify
from tests.fakes import keys


def test_a_key_reads_from_base64_or_base64url_and_junk_is_refused():
    k = keys()
    url = k.public_text.replace("+", "-").replace("/", "_").rstrip("=")
    assert public_key(k.public_text).public_bytes_raw() == k.public.public_bytes_raw()
    assert public_key(url).public_bytes_raw() == k.public.public_bytes_raw()
    for bad in ("", "not base64!", base64.b64encode(b"short").decode()):
        with pytest.raises(RrotorError) as caught:
            public_key(bad)
        assert caught.value.code == "E_CONFIG"


def test_a_signature_checks_or_not_and_junk_is_a_bad_request():
    k = keys()
    sig = k.sign(b"hello")
    assert verify(k.public, sig, b"hello") is True
    assert verify(k.public, sig, b"hellO") is False
    assert verify(k.public, sig.replace("+", "-").replace("/", "_").rstrip("="), b"hello") is True
    with pytest.raises(RrotorError) as caught:
        unb64("***")
    assert (caught.value.code, caught.value.status) == ("E_SIGNATURE", 400)


def test_canonical_is_pythons_sorted_compact_ascii_json():
    assert canonical({"b": 1, "a": {"d": None, "c": "é"}}) == b'{"a":{"c":"\\u00e9","d":null},"b":1}'
