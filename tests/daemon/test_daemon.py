"""The daemon put together: the runner recovered, the local API served, the link and the expiry running, then
stopped."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import rrotor
from rrotord import daemon
from rrotord.config import Config
from rrotord.daemon import Daemon
from rrotord.errors import RrotorError
from rrotord.runner.launch import SandboxLauncher
from rrotord.runner.record import Run
from tests.fakes import LocalLauncher, ready


def config(tmp_path: Path, short_dir: Path, server: str = "http://127.0.0.1:1") -> Config:
    return Config(tmp_path / "data", "local", server, 0, 0, "ab" * 32, Path(rrotor.__file__).parent, short_dir)


def test_the_daemon_wires_its_parts(tmp_path, short_dir):
    d = Daemon(config(tmp_path, short_dir), ready())
    assert isinstance(d.launcher, SandboxLauncher)
    assert d.link.url == "ws://127.0.0.1:1/v1/runtime/link" and d.runner.notify is not None
    s = d.services()
    assert (s.runner, s.link, s.credentials, s.parts, s.licence) == (
        d.runner,
        d.link,
        d.link.credentials,
        d.parts,
        d.licence,
    )
    assert s.fetch is not None and d.parts.key is None and d.licence.key is None
    assert d.deployer.scratch == short_dir / "deploy"


async def test_serve_recovers_runs_and_stops_cleanly(tmp_path, short_dir):
    d = Daemon(config(tmp_path, short_dir), ready(), launcher=LocalLauncher())
    lost = Run(str(uuid.uuid4()), "p", 1, "a" * 40, "start", {}, "claude", "local")
    d.runner.records.save(lost)
    stop = asyncio.Event()
    serving = asyncio.create_task(d.serve(stop))
    for _ in range(100):
        if d.runner.get(lost.id).status == "failed":
            break
        await asyncio.sleep(0.01)
    assert d.runner.get(lost.id).status == "failed" and d.link.state == "no_token"
    stop.set()
    await asyncio.wait_for(serving, 5)
    assert serving.done() and serving.exception() is None


async def test_the_link_connects_through_one_session(tmp_path, short_dir):
    async def link(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/v1/runtime/link", link)
    server = TestServer(app)
    await server.start_server()
    try:
        d = Daemon(config(tmp_path, short_dir, str(server.make_url("")).rstrip("/")), ready(), launcher=LocalLauncher())
        ws = await d._connect(d.link.url, {})
        session = d.session
        await ws.close()
        ws = await d._connect(d.link.url, {})
        assert d.session is session
        await ws.close()
        stop = asyncio.Event()
        stop.set()
        await d.serve(stop)
        assert session is not None and session.closed
    finally:
        await server.close()


async def test_expiry_runs_and_survives_a_failure(tmp_path, short_dir, monkeypatch, caplog):
    monkeypatch.setattr(daemon, "EXPIRE_EVERY", 0.01)
    d = Daemon(config(tmp_path, short_dir), ready(), launcher=LocalLauncher())
    calls = []

    async def expire(at: Any = None) -> list[str]:
        calls.append(at)
        if len(calls) == 1:
            raise RuntimeError("disk full")
        return []

    monkeypatch.setattr(d.runner, "expire", expire)
    task = asyncio.create_task(d._expire())
    for _ in range(200):
        if len(calls) >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(calls) >= 2 and "expiring waiting runs failed" in caplog.text
    assert aiohttp  # the session type the daemon keeps


async def test_a_part_is_fetched_by_url_and_a_refusal_is_an_error(tmp_path, short_dir):
    async def ok(_: web.Request) -> web.Response:
        return web.Response(body=b"tarball bytes")

    async def gone(_: web.Request) -> web.Response:
        return web.Response(status=404)

    app = web.Application()
    app.router.add_get("/ada.tar.gz", ok)
    app.router.add_get("/missing.tar.gz", gone)
    server = TestServer(app)
    await server.start_server()
    d = Daemon(config(tmp_path, short_dir), ready())
    try:
        assert await d._fetch(f"http://127.0.0.1:{server.port}/ada.tar.gz") == b"tarball bytes"
        with pytest.raises(RrotorError) as caught:
            await d._fetch(f"http://127.0.0.1:{server.port}/missing.tar.gz")
        assert (caught.value.code, caught.value.status) == ("E_PART_FETCH", 502)
    finally:
        if d.session is not None:
            await d.session.close()
        await server.close()
