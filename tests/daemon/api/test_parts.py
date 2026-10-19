"""The local API's parts and licence routes."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
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
    assert (body["licence"]["state"], body["licence"]["max_precedents"], body["parts"]) == ("live", 500000, [])
    status, body = await send(api, "PUT", "/v1/licence", data=keys().key(drop=("tier",)))
    assert (status, body["error"]["code"]) == (400, "E_LICENCE") and "tier missing" in body["error"]["message"]


async def test_a_legacy_licence_is_still_handed_in_as_json_its_token_or_token_in_release_n(api):
    offline = keys().licence(["ada"], expires="2998-01-01T00:00:00Z", grace_days=14, kind="offline", plan="Team")
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
        "name": "ada",
        "version": "1.0.0",
        "sha256": sha,
        "signature": sig,
        "archive": base64.b64encode(data).decode(),
    }
    status, body = await send(api, "POST", "/v1/parts", json=ask)
    assert (status, body) == (201, {"name": "ada", "version": "1.0.0", "mod": "ada", "arch": "arm64"})
    status, body = await send(api, "GET", "/v1/parts")
    assert body == {"parts": [{"name": "ada", "version": "1.0.0", "mod": "ada", "arch": "arm64"}]}
    status, body = await send(api, "POST", "/v1/parts", json={**ask, "sha256": "0" * 64})
    assert (status, body["error"]["code"]) == (400, "E_PART_HASH")
    status, body = await send(api, "DELETE", "/v1/parts/ada")
    assert (status, body) == (200, {"removed": "ada"})
    status, body = await send(api, "DELETE", "/v1/parts/ada")
    assert (status, body["error"]["code"]) == (404, "E_PART_MISSING")


async def test_the_request_is_checked_and_a_bad_archive_or_not_a_tarball_is_refused(api, tmp_path):
    for ask, code in (
        ({"name": "ada"}, "E_BAD_REQUEST"),
        ({"name": "ada", "version": "1.0.0", "sha256": "x", "signature": "y", "archive": "***"}, "E_PART_ARCHIVE"),
        (
            {"name": "ada", "version": "1.0.0", "sha256": "x", "signature": "y", "url": "https://x/p.tar.gz"},
            "E_PART_ARCHIVE",
        ),
    ):
        status, body = await send(api, "POST", "/v1/parts", json=ask)
        assert (status, body["error"]["code"]) == (400, code), ask
    _data, _sha, _sig = part_archive(tmp_path)
    plain = base64.b64encode(b"not gzip").decode()

    ask = {
        "name": "ada",
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
            "name": "ada",
            "version": "1.0.0",
            "sha256": sha,
            "signature": sig,
            "url": "https://releases.example/ada.tar.gz",
        }
        status, _body = await send(api, "POST", "/v1/parts", json=ask)
        assert status == 201 and asked == ["https://releases.example/ada.tar.gz"]
    finally:
        await api.client.close()


async def test_a_base_part_installs_without_a_licence(api, tmp_path):
    data, sha, sig = part_archive(tmp_path, name="postgres", mod="rrotor")
    archive = base64.b64encode(data).decode()
    ask = {"name": "postgres", "version": "1.0.0", "sha256": sha, "signature": sig, "archive": archive}
    status, _ = await send(api, "POST", "/v1/parts", json=ask)
    assert status == 201


async def test_an_ada_model_deploys_and_a_changed_encoding_that_cannot_be_reencoded_is_refused(api, tmp_path):
    """Through the API: the part installed, the model deployed from a bundle, a precedent recorded, then a version
    with a new seed over a store that is not a database: refused, and the first version stays active."""
    from rrotor import ada
    from tests.fakes import GitRepo, ada_part

    data, sha, sig = ada_part(tmp_path)
    ask = {
        "name": "ada",
        "version": "3.10.0",
        "sha256": sha,
        "signature": sig,
        "archive": base64.b64encode(data).decode(),
    }
    assert (await send(api, "POST", "/v1/parts", json=ask))[0] == 201
    here = Path(__file__).parent.parent.parent.parent / "examples" / "ada_tickets"
    spec = json.loads((here / "spec.json").read_text())
    repo = GitRepo(tmp_path / "repo")
    first = repo.model(
        json.loads((here / "glyphh.json").read_text()),
        (here / "model.py").read_text(),
        **{"spec.json": json.dumps(spec)},
    )
    bundle = base64.b64encode(repo.bundle(tmp_path / "v1.bundle").read_bytes()).decode()
    status, body = await send(api, "POST", "/v1/models/deploy", json={"bundle": bundle, "commit": first})
    assert status == 201, body
    ticket = {
        "issue": {"body": {"title": "login fails", "area": "auth", "severity": 8, "id": 1}},
        "meta": {"time": {"opened": "2026-10-01T12:00:00Z"}, "owner": {"team": "infra"}},
    }
    status, body = await send(
        api,
        "POST",
        "/v1/runs",
        json={"model": "tickets", "function": "record", "inputs": {"data": ticket, "outcome": "page oncall"}},
    )
    assert (status, body["status"]) == (200, "done"), body
    (api.services.models.data("tickets") / ada.STORE).write_bytes(b"not a database")
    second = repo.model(
        json.loads((here / "glyphh.json").read_text()),
        (here / "model.py").read_text(),
        **{"spec.json": json.dumps({**spec, "seed": 7})},
    )
    bundle = base64.b64encode(repo.bundle(tmp_path / "v2.bundle").read_bytes()).decode()
    status, body = await send(api, "POST", "/v1/models/deploy", json={"bundle": bundle, "commit": second})
    assert (status, body["error"]["code"]) == (422, "E_ADA_ENCODING"), body
    active = api.services.models.active("tickets")
    assert active is not None and active.commit_sha == first and api.services.models.by_sha("tickets", second) is None


async def test_the_licence_shows_the_nodes_precedents_against_the_cap_and_over_after_a_downgrade(api):
    from tests.daemon.test_precedents import model

    model(api.services.models, "tickets", ["ada"], 6)
    status, body = await send(api, "GET", "/v1/licence")
    assert status == 200 and body["licence"]["precedents"] == {"held": 6, "cap": 5000, "over": False}
    status, body = await send(api, "PUT", "/v1/licence", data=keys().key(tier="free", max_precedents=5))
    assert status == 200 and body["licence"]["precedents"] == {"held": 6, "cap": 5, "over": True}  # nothing deleted
    status, body = await send(api, "GET", "/v1/health")
    assert status == 200 and body["licence"]["precedents"]["over"] is True
