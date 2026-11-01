"""The vault: a value kept encrypted under the rrotor's own key (made 0600 once), bound to its program and name, read
back only by name; names checked, values bounded."""

from __future__ import annotations

import json
import stat

import pytest

from rrotord.errors import BadRequest, NotFound, RrotorError
from rrotord.vault import KEY_BYTES, Vault


def test_a_value_is_kept_encrypted_and_read_back(tmp_path):
    vault = Vault(tmp_path)
    vault.set("sales", "API_KEY", "sk-live-0123456789")
    vault.set("sales", "OTHER", "two")
    key = tmp_path / "vault" / "key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600 and len(key.read_bytes()) == KEY_BYTES
    sealed = tmp_path / "vault" / "sales.json"
    assert stat.S_IMODE(sealed.stat().st_mode) == 0o600
    assert b"sk-live" not in sealed.read_bytes() and set(json.loads(sealed.read_text())) == {"API_KEY", "OTHER"}
    assert vault.names("sales") == ["API_KEY", "OTHER"] and vault.names("other") == []
    again = Vault(tmp_path)  # the same key, read from its file
    assert again.values("sales", ["API_KEY", "MISSING", "OTHER"]) == (
        {"API_KEY": "sk-live-0123456789", "OTHER": "two"},
        ["MISSING"],
    )
    assert key.read_bytes() == (tmp_path / "vault" / "key").read_bytes()


def test_set_again_replaces_and_delete_removes(tmp_path):
    vault = Vault(tmp_path)
    vault.set("sales", "API_KEY", "one")
    vault.set("sales", "API_KEY", "two")
    assert vault.values("sales", ["API_KEY"])[0] == {"API_KEY": "two"}
    vault.set("sales", "OTHER", "x")
    vault.delete("sales", "API_KEY")
    assert vault.names("sales") == ["OTHER"]
    vault.delete("sales", "OTHER")
    assert not (tmp_path / "vault" / "sales.json").exists()
    with pytest.raises(NotFound) as gone:
        vault.delete("sales", "OTHER")
    assert gone.value.code == "E_SECRET_MISSING"


def test_a_value_moved_to_another_name_or_program_does_not_open(tmp_path):
    vault = Vault(tmp_path)
    vault.set("sales", "API_KEY", "secret-value")
    sealed = json.loads((tmp_path / "vault" / "sales.json").read_text())
    (tmp_path / "vault" / "sales.json").write_text(json.dumps({"OTHER": sealed["API_KEY"]}))
    with pytest.raises(RrotorError) as moved:
        vault.values("sales", ["OTHER"])
    assert moved.value.code == "E_VAULT" and "secret-value" not in moved.value.message
    (tmp_path / "vault" / "ledger.json").write_text(json.dumps({"API_KEY": sealed["API_KEY"]}))
    with pytest.raises(RrotorError):
        vault.values("ledger", ["API_KEY"])
    (tmp_path / "vault" / "ledger.json").write_text(json.dumps({"API_KEY": "not base64!"}))
    with pytest.raises(RrotorError):
        vault.values("ledger", ["API_KEY"])


def test_another_key_opens_nothing_and_a_damaged_key_is_refused(tmp_path):
    vault = Vault(tmp_path)
    vault.set("sales", "API_KEY", "v")
    (tmp_path / "vault" / "key").write_bytes(b"k" * KEY_BYTES)
    with pytest.raises(RrotorError) as other:
        Vault(tmp_path).values("sales", ["API_KEY"])
    assert other.value.code == "E_VAULT"
    (tmp_path / "vault" / "key").write_bytes(b"short")
    with pytest.raises(RrotorError) as damaged:
        Vault(tmp_path).set("sales", "API_KEY", "v")
    assert damaged.value.code == "E_VAULT"


def test_a_file_that_is_not_an_object_holds_nothing(tmp_path):
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "sales.json").write_text("[1]")
    assert Vault(tmp_path).names("sales") == []


@pytest.mark.parametrize(
    ("program", "name", "value", "code"),
    [
        ("Sales", "API_KEY", "v", "E_BAD_REQUEST"),
        ("../x", "API_KEY", "v", "E_BAD_REQUEST"),
        ("sales", "api_key", "v", "E_SECRET_NAME"),
        ("sales", "1KEY", "v", "E_SECRET_NAME"),
        ("sales", "API_KEY", "", "E_SECRET_VALUE"),
        ("sales", "API_KEY", "x" * (64 * 1024 + 1), "E_SECRET_VALUE"),
    ],
)
def test_names_and_values_are_checked(tmp_path, program, name, value, code):
    with pytest.raises(BadRequest) as refused:
        Vault(tmp_path).set(program, name, value)
    assert refused.value.code == code


def test_a_bad_name_is_refused_on_delete(tmp_path):
    with pytest.raises(BadRequest):
        Vault(tmp_path).delete("sales", "lower")
