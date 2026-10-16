from __future__ import annotations

import base64
import uuid
from typing import Any

import pytest

from rrotord.api import ops, routes
from rrotord.sandbox.probe import Layers
from tests.daemon.api.conftest import Api
from tests.fakes import GitRepo, install, manifest, token

WAITS = """
from rrotor import Result

def start(ctx, inputs):
    if ctx.ask_approval("write", "Write it?"):
        return Result.done("written")
    return Result.done("declined")
"""


async def send(api: Api, method: str, path: str, **kw: Any) -> tuple[int, Any]:
    res = await api.client.request(method, path, headers=api.auth(), **kw)
    return res.status, await res.json()


def deploy_sample(api: Api, source: str = WAITS) -> None:
    install(api.services.models, manifest("sample", functions={"start": {"approvals": ["write"]}}), source)


async def test_health_shows_the_runtime(api):
    status, body = await send(api, "GET", "/v1/health")
    assert status == 200
    assert (body["mode"], body["runtime_id"], body["link"], body["approvals"], body["models"]) == (
        "local",
        None,
        {"state": "no_token"},
        0,
        0,
    )
    assert body["sandbox"]["ready"] and "browser" not in body


async def test_the_runtime_token_is_handed_in(api):
    rid = str(uuid.uuid4())
    status, body = await send(api, "PUT", "/v1/runtime/token", json={"runtime_id": rid, "token": token()})
    assert (status, body) == (200, {"runtime_id": rid, "link": "connecting"})
    assert api.services.credentials.changed.is_set()


async def test_a_bad_runtime_token_is_refused(api):
    status, body = await send(api, "PUT", "/v1/runtime/token", json={"runtime_id": "x", "token": token()})
    assert (status, body["error"]["code"]) == (400, "E_RUNTIME_ID")


@pytest.mark.parametrize("data", [b"not json", b"[1]", b"\xff"])
async def test_a_body_that_is_not_a_json_object(api, data):
    status, body = await send(api, "PUT", "/v1/runtime/token", data=data)
    assert (status, body["error"]["code"]) == (400, "E_BAD_REQUEST")


async def test_models_lists_what_is_deployed(api):
    deploy_sample(api)
    status, body = await send(api, "GET", "/v1/models")
    assert status == 200 and body["models"][0]["name"] == "sample"


async def test_deploy_a_bundle(api, tmp_path):
    repo = GitRepo(tmp_path / "src")
    sha = repo.model(manifest("sample"))
    bundle = base64.b64encode(repo.bundle(tmp_path / "b.bundle").read_bytes()).decode()
    status, body = await send(api, "POST", "/v1/models/deploy", json={"bundle": bundle, "commit": sha})
    assert (status, body["model"], body["version"], body["commit_sha"]) == (201, "sample", 1, sha)
    assert list((api.root / "run" / "bundles").iterdir()) == []


async def test_deploy_a_bundle_that_is_not_base64(api):
    status, body = await send(api, "POST", "/v1/models/deploy", json={"bundle": "%%%", "commit": "a" * 40})
    assert (status, body["error"]["code"]) == (400, "E_BUNDLE")


async def test_deploy_a_bundle_too_big(api, monkeypatch):
    monkeypatch.setattr(ops, "BUNDLE_MAX", 2)
    status, body = await send(api, "POST", "/v1/models/deploy", json={"bundle": "AAAA", "commit": "a" * 40})
    assert (status, body["error"]["code"]) == (400, "E_TOO_BIG")


async def test_deploy_a_bundle_that_git_refuses_leaves_no_file(api):
    status, body = await send(api, "POST", "/v1/models/deploy", json={"bundle": "AAAA", "commit": "a" * 40})
    assert (status, body["error"]["code"]) == (422, "E_GIT")
    assert list((api.root / "run" / "bundles").iterdir()) == []


async def test_deploy_from_a_repo(api, tmp_path):
    repo = GitRepo(tmp_path / "src")
    sha = repo.model(manifest("sample"))
    repo.bare(tmp_path / "github", "owner/sample")
    status, body = await send(
        api, "POST", "/v1/models/deploy", json={"repo": "owner/sample", "commit": sha, "token": token()}
    )
    assert (status, body["commit_sha"]) == (201, sha)


@pytest.mark.parametrize(
    "data",
    [
        {"repo": "owner/x"},
        {"commit": "a" * 40},
        {"commit": "a" * 40, "repo": "owner/x", "token": 5},
        {"commit": "a" * 40, "repo": 1},
    ],
)
async def test_deploy_with_the_wrong_fields(api, data):
    status, body = await send(api, "POST", "/v1/models/deploy", json=data)
    assert (status, body["error"]["code"]) == (400, "E_BAD_REQUEST")


async def test_a_run_through_the_api(api):
    deploy_sample(api)
    status, first = await send(api, "POST", "/v1/runs", json={"model": "sample", "inputs": {}})
    assert (status, first["status"], first["step"], first["prompt"]) == (200, "needs_approval", "write", "Write it?")
    run_id = first["run_id"]
    approvals = (await send(api, "GET", "/v1/approvals"))[1]["approvals"]
    assert approvals[0]["run_id"] == run_id
    assert (await send(api, "GET", "/v1/health"))[1]["approvals"] == 1
    status, done = await send(api, "POST", f"/v1/runs/{run_id}/continue", json={"answer": {"approved": True}})
    assert (done["status"], done["data"]) == ("done", "written")
    status, view = await send(api, "GET", f"/v1/runs/{run_id}")
    assert (view["status"], view["function"]) == ("done", "start")
    status, log = await send(api, "GET", f"/v1/runs/{run_id}/log")
    assert log["run_id"] == run_id and log["lines"][0]["event"] == "called"


async def test_cancel_through_the_api(api):
    deploy_sample(api)
    run_id = (await send(api, "POST", "/v1/runs", json={"model": "sample", "function": "start", "version": 1}))[1][
        "run_id"
    ]
    status, body = await send(api, "POST", f"/v1/runs/{run_id}/cancel")
    assert (status, body["status"]) == (200, "cancelled")
    status, body = await send(api, "POST", f"/v1/runs/{run_id}/cancel")
    assert (status, body["error"]["code"]) == (409, "E_RUN_STATE")


@pytest.mark.parametrize(
    "data",
    [{}, {"model": "x", "function": 1}, {"model": "x", "inputs": [1]}, {"model": "x", "version": "1"}],
)
async def test_a_call_with_the_wrong_fields(api, data):
    status, body = await send(api, "POST", "/v1/runs", json=data)
    assert (status, body["error"]["code"]) == (400, "E_BAD_REQUEST")


async def test_a_run_not_here(api):
    status, body = await send(api, "GET", "/v1/runs/nope")
    assert (status, body["error"]["code"]) == (404, "E_RUN_MISSING")
    assert (await send(api, "GET", "/v1/runs/nope/log"))[0] == 404


async def test_calls_are_refused_while_the_sandbox_is_not_ready(api):
    api.services.runner.layers = Layers({"bubblewrap": {"ok": False, "detail": "not installed"}})
    deploy_sample(api)
    status, body = await send(api, "POST", "/v1/runs", json={"model": "sample"})
    assert (status, body["error"]["code"]) == (503, "E_SANDBOX_UNAVAILABLE")


async def test_a_call_carries_clearance_and_refuses_one_out_of_range(api):
    install(
        api.services.models,
        manifest("sample"),
        "from rrotor import Result\ndef start(ctx, inputs):\n    return Result.done(ctx.clearance)\n",
    )
    status, body = await send(api, "POST", "/v1/runs", json={"model": "sample", "clearance": 0.4})
    assert (status, body["data"]) == (200, 0.4)
    status, body = await send(api, "POST", "/v1/runs", json={"model": "sample"})
    assert (status, body["data"]) == (200, 1.0)
    for bad in (2, -0.1, "1", True):
        status, body = await send(api, "POST", "/v1/runs", json={"model": "sample", "clearance": bad})
        assert (status, body["error"]["code"]) == (400, "E_BAD_REQUEST"), bad
