"""The work routes over HTTP: who gets in (the bearers every route takes, or a work ticket, on these paths only), the
platform's page asking from the browser (CORS, its preflight), and each route's answer."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from rrotord.access import Access, digest
from rrotord.api import forward
from rrotord.api.app import make
from rrotord.config import PLATFORM
from tests.daemon.api.conftest import PORT, Api, services
from tests.fakes import b64url, git, keys, token

RUNTIME = "0b0e3a52-6a55-4d0b-9d52-0f6f4f1c2a11"
WS = "acme__crew"
BRANCH = "turn-1-ada-k3x9"
HOST = "3d8f1a2b3c4d5e.glyphh.app"


def ticket(**fields: Any) -> str:
    payload = {"kind": "work", "runtime_id": RUNTIME, "sub": "user_1", "expires_at": "2999-01-01T00:00:00Z"} | fields
    raw = json.dumps(payload).encode()
    return f"{b64url(raw)}.{b64url(keys().private.sign(raw))}"


def bearer(value: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}", **extra}


def clone(root: Path) -> Path:
    """acme/crew as glyphh_swarm keeps it under this rrotor's data: main, and a turn's branch one file ahead."""
    origin = root / "origin.git"
    origin.mkdir()
    git(origin, "init", "-q", "--bare", "-b", "main")
    folder = root / "data" / "programs" / "glyphh_swarm" / "data" / "repos" / WS
    folder.parent.mkdir(parents=True)
    git(root, "clone", "-q", str(origin), str(folder))
    (folder / "README.md").write_text("# crew\n")
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "Start")
    git(folder, "push", "-q", "origin", "HEAD:main")
    git(folder, "checkout", "-q", "-b", BRANCH)
    (folder / "hello.txt").write_text("hello\n")
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "Ada: hello")
    git(folder, "checkout", "-q", "main")
    return folder


@pytest.fixture
async def work(api: Api) -> Api:
    """A local rrotor that knows its runtime id (its link's credentials), with the crew's clone on it."""
    api.services.credentials.set(RUNTIME, token())
    clone(api.root)
    return api


async def got(api: Api, path: str, **headers: str) -> tuple[int, Any]:
    res = await api.client.get(path, headers=headers)
    return res.status, await res.json()


async def test_the_local_token_reads_the_work(work):
    status, body = await got(work, "/v1/work", **work.auth())
    assert status == 200 and [w["id"] for w in body["workspaces"]] == [WS]
    assert body["workspaces"][0]["name"] == "acme/crew" and body["workspaces"][0]["branch"] == "main"
    status, body = await got(work, f"/v1/work/{WS}/tree", **work.auth())
    assert status == 200 and [e["name"] for e in body["entries"]] == ["README.md"]
    status, body = await got(work, f"/v1/work/{WS}/tree?under=nowhere", **work.auth())
    assert (status, body["error"]["code"]) == (404, "E_NOT_FOUND")
    res = await work.client.get("/v1/work", headers=work.auth())
    assert res.headers["Cache-Control"] == "no-store"


async def test_a_work_ticket_lets_the_person_in_on_the_work_routes_only(work):
    status, body = await got(work, "/v1/work", **bearer(ticket()))
    assert status == 200 and len(body["workspaces"]) == 1
    assert (await got(work, f"/v1/work/{WS}/tree", **bearer(ticket())))[0] == 200
    for path in ("/v1/health", "/v1/programs", "/v1/runs/x"):
        status, body = await got(work, path, **bearer(ticket()))
        assert (status, body["error"]["code"]) == (401, "E_AUTH"), path
    for bad in (ticket(runtime_id="another"), ticket(expires_at="2020-01-01T00:00:00Z"), ticket(kind="x"), token()):
        assert (await got(work, "/v1/work", **bearer(bad)))[0] == 401
    assert (await got(work, "/v1/work"))[0] == 401
    status, body = await got(work, "/v1/work", **bearer(ticket(), Host="evil.example.com"))
    assert (status, body["error"]["code"]) == (403, "E_HOST")  # a ticket does not move the loopback rule


async def test_a_rrotor_with_no_runtime_id_takes_no_ticket(api):
    assert api.services.credentials.runtime_id is None
    assert (await got(api, "/v1/work", **bearer(ticket())))[0] == 401
    assert (await got(api, "/v1/work", **api.auth())) == (200, {"workspaces": []})


async def test_the_platforms_page_reads_from_the_browser_and_no_other_page_does(work):
    page = {"Origin": PLATFORM}
    res = await work.client.options(
        f"/v1/work/{WS}/file", headers=page | {"Access-Control-Request-Method": "PUT", "Host": f"127.0.0.1:{PORT}"}
    )
    assert res.status == 204 and res.headers["Access-Control-Allow-Origin"] == PLATFORM
    assert res.headers["Access-Control-Allow-Methods"] == "GET, PUT"
    assert res.headers["Access-Control-Allow-Headers"] == "Authorization, Content-Type"
    assert res.headers["Access-Control-Allow-Private-Network"] == "true" and res.headers["Vary"] == "Origin"
    res = await work.client.options("/v1/folders", headers=page)
    assert res.status == 204 and res.headers["Access-Control-Allow-Methods"] == "GET"
    assert res.headers["Access-Control-Allow-Headers"] == "Authorization"
    res = await work.client.get("/v1/work", headers=bearer(ticket(), **page))
    assert res.status == 200 and res.headers["Access-Control-Allow-Origin"] == PLATFORM
    res = await work.client.get("/v1/work", headers=page)  # a refusal says so to the page too
    assert res.status == 401 and res.headers["Access-Control-Allow-Origin"] == PLATFORM
    for path, method in (("/v1/work", "POST"), ("/v1/folders", "PUT"), ("/v1/health", "GET"), ("/v1/workers", "GET")):
        res = await work.client.request(method, path, headers=work.auth(**page))
        assert res.status == 403 and "Access-Control-Allow-Origin" not in res.headers, (path, method)
        assert (await res.json())["error"]["code"] == "E_ORIGIN"
    res = await work.client.get("/v1/work", headers=work.auth(Origin="https://evil.example.com"))
    assert res.status == 403 and "Access-Control-Allow-Origin" not in res.headers


async def test_a_file_is_read_as_text_or_as_its_bytes(work):
    status, body = await got(work, f"/v1/work/{WS}/file?path=README.md", **work.auth())
    assert status == 200
    assert body == {"path": "README.md", "size": 7, "binary": False, "text": "# crew\n", "truncated": False}
    status, body = await got(work, f"/v1/work/{WS}/file?path=hello.txt&ref={BRANCH}", **work.auth())
    assert status == 200 and body["text"] == "hello\n"
    res = await work.client.get(f"/v1/work/{WS}/file?path=hello.txt&ref={BRANCH}&raw=1", headers=work.auth())
    assert res.status == 200 and await res.read() == b"hello\n" and res.content_type == "text/plain"
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["Content-Security-Policy"] == "sandbox" and res.headers["Cache-Control"] == "no-store"
    for query in ("path=hello.txt", "path=../x", "", "path=.git/config"):
        status, body = await got(work, f"/v1/work/{WS}/file?{query}", **work.auth())
        assert status in (400, 404) and body["error"]["code"] in ("E_PATH", "E_NOT_FOUND"), query


async def test_a_file_is_saved_and_the_save_is_an_audit_event(work):
    url = f"/v1/work/{WS}/file?path=docs/plan.md"
    res = await work.client.put(url, json={"text": "# plan\n"}, headers=bearer(ticket(), Origin=PLATFORM))
    assert res.status == 200 and await res.json() == {"path": "docs/plan.md", "size": 7}
    assert res.headers["Access-Control-Allow-Origin"] == PLATFORM
    folder = work.root / "data" / "programs" / "glyphh_swarm" / "data" / "repos" / WS
    assert (folder / "docs" / "plan.md").read_text() == "# plan\n"
    bodies: tuple[dict[str, Any], ...] = (
        {"data": "not json"},
        {"json": ["a list"]},
        {"json": {"text": 7}},
        {"json": {}},
    )
    for sent in bodies:
        res = await work.client.put(url, headers=work.auth(), **sent)
        assert res.status == 400, sent
    res = await work.client.put(f"/v1/work/{WS}/file?path=.git/config", json={"text": "x"}, headers=work.auth())
    assert res.status == 400 and (await res.json())["error"]["code"] == "E_PATH"

    events: list[dict[str, Any]] = []
    work.services.audit = cast(
        Any, SimpleNamespace(emit=lambda kind, **detail: events.append({"kind": kind, **detail}))
    )
    res = await work.client.put(url, json={"text": "v2\n"}, headers=bearer(ticket()))
    assert res.status == 200
    assert events == [
        {
            "kind": "work.write",
            "workspace": WS,
            "path": "docs/plan.md",
            "size": 3,
            "outcome": {"status": "done", "code": None},
        }
    ]


async def test_the_diff_and_the_command_log(work):
    status, body = await got(work, f"/v1/work/{WS}/diff?branch={BRANCH}", **work.auth())
    assert status == 200 and (body["base"], body["branch"], body["truncated"]) == ("main", BRANCH, False)
    assert body["files"] == [
        {
            "path": "hello.txt",
            "status": "added",
            "added": 1,
            "removed": 0,
            "binary": False,
            "before": None,
            "after": "hello\n",
        }
    ]
    status, body = await got(work, f"/v1/work/{WS}/diff?base=main", **work.auth())
    assert status == 200 and body["files"] == [] and body["branch"] is None
    assert (await got(work, f"/v1/work/{WS}/diff?branch=nope", **work.auth()))[0] == 404
    assert (await got(work, "/v1/work/acme__other/diff", **work.auth()))[0] == 404

    log = work.root / "data" / "programs" / "glyphh_swarm" / "data" / "terminal" / f"{WS}.jsonl"
    log.parent.mkdir()
    log.write_text(json.dumps({"n": 0, "at": "2026-11-22T10:00:00+00:00", "kind": "cmd", "text": "git fetch"}) + "\n")
    status, body = await got(work, f"/v1/work/{WS}/terminal", **work.auth())
    assert status == 200 and body["next"] == 1 and body["live"] is False
    assert body["lines"] == [{"n": 0, "at": "2026-11-22T10:00:00+00:00", "kind": "cmd", "text": "git fetch"}]
    assert (await got(work, f"/v1/work/{WS}/terminal?after=1", **work.auth()))[1]["lines"] == []
    status, body = await got(work, f"/v1/work/{WS}/terminal?after=-1", **work.auth())
    assert (status, body["error"]["code"]) == (400, "E_BAD_REQUEST")


async def test_a_clusters_follower_sends_the_caller_to_its_head(work, monkeypatch):
    monkeypatch.setattr(forward, "follower", lambda s: True)
    paths = ("", f"/{WS}/tree", f"/{WS}/file?path=README.md", f"/{WS}/diff", f"/{WS}/terminal")
    for path in paths:
        status, body = await got(work, f"/v1/work{path}", **work.auth())
        assert (status, body["error"]["code"]) == (409, "E_HEAD_ONLY"), path
    res = await work.client.put(f"/v1/work/{WS}/file?path=x", json={"text": "x"}, headers=work.auth())
    assert res.status == 409


@pytest.fixture
async def hosted(tmp_path: Path, short_dir: Path) -> AsyncIterator[Api]:
    key = token()
    s = services(tmp_path, short_dir, token())
    s.config = dataclasses.replace(
        s.config, mode="hosted", machine_id=HOST.split(".")[0], local_token_sha256=None, runtime_id=RUNTIME
    )
    s.access = Access(s.config.data, (digest(key),))
    client: TestClient[web.Request, web.Application] = TestClient(TestServer(make(s)), headers={"Host": HOST})
    await client.start_server()
    yield Api(client, s, key, tmp_path)
    await s.runner.shutdown()
    await client.close()


async def test_a_hosted_rrotor_takes_a_ticket_for_its_runtime_on_the_work_routes_only(hosted):
    assert (await got(hosted, "/v1/work", **bearer(ticket(), Origin=PLATFORM))) == (200, {"workspaces": []})
    assert (await got(hosted, "/v1/work", **hosted.auth())) == (200, {"workspaces": []})
    assert (await got(hosted, "/v1/health", **bearer(ticket())))[0] == 401
    assert (await got(hosted, "/v1/work", **bearer(ticket(runtime_id="another"))))[0] == 401
    assert hosted.services.access is not None
    hosted.services.access.pause("runtime_hours")
    assert (await got(hosted, "/v1/work", **bearer(ticket())))[0] == 402  # a paused runtime is paused for all
