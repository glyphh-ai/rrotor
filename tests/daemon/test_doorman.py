"""The doorman: every request for a machine's address is replayed to that machine; health is its own; anything else
is misdirected."""

from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

import rrotor
from rrotord import doorman
from rrotord.config import Config, load


def _config(tmp_path: Path) -> Config:
    return load({"RROTOR_MODE": "doorman", "RROTOR_DATA": str(tmp_path), "RROTOR_RUN_DIR": str(tmp_path / "run")})


async def test_requests_are_replayed_by_host(tmp_path):
    c = _config(tmp_path)
    assert (c.mode, c.public_domain, c.sdk) == ("doorman", "glyphh.app", Path(rrotor.__file__).parent)
    async with TestClient(TestServer(doorman.make(c))) as client:
        res = await client.post("/v1/runs", headers={"Host": "3d8f1a2b3c4d5e.glyphh.app"}, json={})
        assert res.status == 200 and res.headers["fly-replay"] == "instance=3d8f1a2b3c4d5e"
        res = await client.get("/mcp", headers={"Host": "ABCDEF123456.GLYPHH.APP:443"})
        assert res.headers["fly-replay"] == "instance=abcdef123456"
        res = await client.get("/healthz", headers={"Host": "anything.example.com"})
        assert res.status == 200 and (await res.json())["doorman"] is True
        for host in ("glyphh.app", "evil.example.com", "a.b.glyphh.app", "x.glyphh.app", "glyphh-runtimes.fly.dev"):
            res = await client.get("/v1/health", headers={"Host": host})
            assert res.status == 421 and (await res.json())["error"]["code"] == "E_HOST", host


async def test_serve_runs_until_stopped(tmp_path):
    stop = asyncio.Event()
    stop.set()
    await doorman.serve(_config(tmp_path), stop)
