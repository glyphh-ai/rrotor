import stat
import uuid

import pytest

from rrotord.errors import BadRequest
from rrotord.link.credentials import Credentials, digest
from tests.fakes import token


def test_nothing_handed_in_yet(tmp_path):
    c = Credentials(tmp_path)
    assert (c.runtime_id, c.token, c.changed.is_set()) == (None, None, False)


def test_set_keeps_them_private_and_says_so(tmp_path):
    rid, secret = str(uuid.uuid4()), token()
    c = Credentials(tmp_path)
    c.set(rid.upper(), secret)
    assert (c.runtime_id, c.token, c.changed.is_set()) == (rid, secret, True)
    assert stat.S_IMODE((tmp_path / "runtime.json").stat().st_mode) == 0o600
    again = Credentials(tmp_path)
    assert (again.runtime_id, again.token) == (rid, secret)


@pytest.mark.parametrize("rid", ["nope", None, 5])
def test_a_runtime_id_that_is_not_a_uuid(tmp_path, rid):
    with pytest.raises(BadRequest) as caught:
        Credentials(tmp_path).set(rid, token())
    assert caught.value.code == "E_RUNTIME_ID"


@pytest.mark.parametrize("bad", ["short", "has spaces in it!!!!", None])
def test_a_token_that_is_not_one(tmp_path, bad):
    with pytest.raises(BadRequest) as caught:
        Credentials(tmp_path).set(str(uuid.uuid4()), bad)
    assert caught.value.code == "E_TOKEN"


def test_digest_is_sha256_hex():
    assert digest("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
