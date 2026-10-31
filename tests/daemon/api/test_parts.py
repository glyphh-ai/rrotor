"""The local API's parts and licence routes."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from tests.daemon.api.conftest import open_api
from tests.daemon.api.test_routes import send
from tests.fakes import keys, part_archive


async def test_no_key_is_community_and_a_key_is_handed_in_as_the_body_and_shown(api):
    status, body = await send(api, "GET", "/v1/licence")
    assert status == 200 and (body["licence"]["state"], body["licence"]["tier"], body["licence"]["community"]) == (
        "none",
        "free",
        True,
    )
    key = keys().key(tier="team", max_nodes=10, max_precedents=500000)
    status, body = await send(api, "PUT", "/v1/licence", data=key + "\n")
    assert (status, body["kept"], body["licence"]["tier"], body["licence"]["source"]) == (200, True, "team", "api")
    status, body = await send(api, "PUT", "/v1/licence", json=keys().key(issued_at="2026-01-01T00:00:00Z"))
    assert (status, body["kept"], body["licence"]["tier"]) == (200, False, "team")  # older: the newer one stays
    status, body = await send(api, "GET", "/v1/health")
    assert (body["licence"]["state"], body["licence"]["max_nodes"], body["parts"]) == ("live", 10, [])
    assert "max_precedents" not in body["licence"]  # Ada Cloud's limit: not rrotor's
    status, body = await send(api, "PUT", "/v1/licence", data=keys().key(drop=("tier",)))
    assert (status, body["error"]["code"]) == (400, "E_LICENCE") and "tier missing" in body["error"]["message"]


async def test_a_legacy_licence_is_still_handed_in_as_json_its_token_or_token_in_release_n(api):
    offline = keys().licence(["rrotor"], expires="2998-01-01T00:00:00Z", grace_days=14, kind="offline", plan="Team")
    status, body = await send(api, "PUT", "/v1/licence", data=keys().token(offline) + "\n")
    assert (status, body["kept"], body["licence"]["tier"], body["licence"]["state"]) == (200, True, "team", "live")
    status, body = await send(api, "PUT", "/v1/licence", json={"token": keys().token(offline)})
    assert (status, body["kept"]) == (200, True)
    status, body = await send(api, "PUT", "/v1/licence", json=keys().licence([], expires="2997-01-01T00:00:00Z"))
    assert (status, body["kept"], body["licence"]["tier"]) == (200, False, "team")  # the longer one stays
    for bad in ({"payload": {}, "signature": "x"}, [1]):
        status, body = await send(api, "PUT", "/v1/licence", json=bad)
        assert (status, body["error"]["code"]) == (400, "E_LICENCE")
    status, body = await send(api, "PUT", "/v1/licence", data="not a token")
    assert (status, body["error"]["code"]) == (400, "E_LICENCE")


async def test_a_part_installs_by_archive_with_no_licence_at_all(api, tmp_path):
    data, sha, sig = part_archive(tmp_path)
    ask: dict[str, Any] = {
        "name": "tools",
        "version": "1.0.0",
        "sha256": sha,
        "signature": sig,
        "archive": base64.b64encode(data).decode(),
    }
    status, body = await send(api, "POST", "/v1/parts", json=ask)
    assert (status, body) == (201, {"name": "tools", "version": "1.0.0", "mod": "tools", "arch": "arm64"})
    status, body = await send(api, "GET", "/v1/parts")
    assert body == {"parts": [{"name": "tools", "version": "1.0.0", "mod": "tools", "arch": "arm64"}]}
    status, body = await send(api, "POST", "/v1/parts", json={**ask, "sha256": "0" * 64})
    assert (status, body["error"]["code"]) == (400, "E_PART_HASH")
    status, body = await send(api, "DELETE", "/v1/parts/tools")
    assert (status, body) == (200, {"removed": "tools"})
    status, body = await send(api, "DELETE", "/v1/parts/tools")
    assert (status, body["error"]["code"]) == (404, "E_PART_MISSING")


async def test_the_request_is_checked_and_a_bad_archive_or_not_a_tarball_is_refused(api, tmp_path):
    for ask, code in (
        ({"name": "tools"}, "E_BAD_REQUEST"),
        ({"name": "tools", "version": "1.0.0", "sha256": "x", "signature": "y", "archive": "***"}, "E_PART_ARCHIVE"),
        (
            {"name": "tools", "version": "1.0.0", "sha256": "x", "signature": "y", "url": "https://x/p.tar.gz"},
            "E_PART_ARCHIVE",
        ),
    ):
        status, body = await send(api, "POST", "/v1/parts", json=ask)
        assert (status, body["error"]["code"]) == (400, code), ask
    _data, _sha, _sig = part_archive(tmp_path)
    plain = base64.b64encode(b"not gzip").decode()

    ask = {
        "name": "tools",
        "version": "1.0.0",
        "sha256": hashlib.sha256(b"not gzip").hexdigest(),
        "signature": keys().sign(b"not gzip"),
        "archive": plain,
    }
    status, body = await send(api, "POST", "/v1/parts", json=ask)
    assert (status, body["error"]["code"]) == (400, "E_PART_MANIFEST")


async def test_a_part_by_url_is_fetched_when_the_daemon_can(tmp_path, short_dir):
    data, sha, sig = part_archive(tmp_path)
    asked: list[str] = []

    async def fetch(url: str) -> bytes:
        asked.append(url)
        return data

    api = await open_api(tmp_path, short_dir, fetch)
    try:
        ask = {
            "name": "tools",
            "version": "1.0.0",
            "sha256": sha,
            "signature": sig,
            "url": "https://releases.example/tools.tar.gz",
        }
        status, _body = await send(api, "POST", "/v1/parts", json=ask)
        assert status == 201 and asked == ["https://releases.example/tools.tar.gz"]
    finally:
        await api.client.close()


async def test_a_base_part_installs_without_a_licence(api, tmp_path):
    data, sha, sig = part_archive(tmp_path, name="postgres", mod="rrotor")
    archive = base64.b64encode(data).decode()
    ask = {"name": "postgres", "version": "1.0.0", "sha256": sha, "signature": sig, "archive": archive}
    status, _ = await send(api, "POST", "/v1/parts", json=ask)
    assert status == 201
