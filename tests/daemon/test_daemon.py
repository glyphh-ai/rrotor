"""The daemon put together: the runner recovered, the local API served, the link and the expiry running, then
stopped."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import signal
import uuid
from datetime import UTC, datetime, timedelta
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
from rrotord.link.credentials import digest
from rrotord.runner.launch import SandboxLauncher
from rrotord.runner.record import Run
from rrotord.sandbox.probe import Layers
from tests.fakes import LocalLauncher, keys, part_archive, ready


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
    assert s.fetch is not None and d.parts.key is None and d.licence.state == "none"
    assert s.report == d.handler.report and d.handler.link is d.link
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
    app.router.add_get("/tools.tar.gz", ok)
    app.router.add_get("/missing.tar.gz", gone)
    server = TestServer(app)
    await server.start_server()
    d = Daemon(config(tmp_path, short_dir), ready())
    try:
        assert await d._fetch(f"http://127.0.0.1:{server.port}/tools.tar.gz") == b"tarball bytes"
        with pytest.raises(RrotorError) as caught:
            await d._fetch(f"http://127.0.0.1:{server.port}/missing.tar.gz")
        assert (caught.value.code, caught.value.status) == ("E_PART_FETCH", 502)
    finally:
        if d.session is not None:
            await d.session.close()
        await server.close()


def test_a_hosted_daemon_takes_its_credentials_from_its_config_once(tmp_path, short_dir):
    runtime_id = str(uuid.uuid4())
    c = Config(tmp_path / "data", "hosted", "http://127.0.0.1:1", 0, 0, None, Path(rrotor.__file__).parent, short_dir)
    assert Daemon(c, ready()).link.credentials.token is None
    hosted = dataclasses.replace(c, runtime_id=runtime_id, runtime_token="tok_" + "a" * 20)
    d = Daemon(hosted, ready())
    assert (d.link.credentials.runtime_id, d.link.credentials.token) == (runtime_id, "tok_" + "a" * 20)
    assert d.access.keys == [] and d.services().access is d.access
    keyed = dataclasses.replace(hosted, data=tmp_path / "keyed", keys_sha256=("a" * 64,))
    assert Daemon(keyed, ready()).access.keys == ["a" * 64]
    again = Daemon(hosted, ready())  # the same credentials: nothing rewritten, the link not restarted
    assert again.link.credentials.token == "tok_" + "a" * 20 and not again.link.credentials.changed.is_set()
    checks = d.services().introspect
    assert checks is not None and checks.url == "http://127.0.0.1:1/v1/runtime/introspect"
    assert (
        checks.credentials is d.link.credentials
        and Daemon(config(tmp_path, short_dir), ready()).services().introspect is None
    )


async def test_a_sign_in_is_asked_of_the_cloud_as_json(tmp_path, short_dir):
    seen = []

    async def introspect(request: web.Request) -> web.Response:
        seen.append((request.headers.get("Authorization"), await request.json()))
        return web.json_response({"active": True})

    async def broken(_: web.Request) -> web.Response:
        return web.Response(status=502, text="bad gateway")

    async def token_endpoint(request: web.Request) -> web.Response:
        form = await request.post()
        seen.append((request.content_type, dict(form)))
        return web.json_response({"access_token": "at"})

    app = web.Application()
    app.router.add_post("/v1/runtime/introspect", introspect)
    app.router.add_post("/broken", broken)
    app.router.add_post("/oauth/token", token_endpoint)
    server = TestServer(app)
    await server.start_server()
    d = Daemon(config(tmp_path, short_dir), ready())
    try:
        url = f"http://127.0.0.1:{server.port}"
        assert await d._post_form(f"{url}/oauth/token", {}, {"grant_type": "x"}) == (200, {"access_token": "at"})
        assert seen == [("application/x-www-form-urlencoded", {"grant_type": "x"})]
        assert await d._post_form(f"{url}/broken", {}, {}) == (502, None)
        assert await d._post(f"{url}/v1/runtime/introspect", {"Authorization": "Bearer r"}, {"token": "t"}) == (
            200,
            {"active": True},
        )
        assert seen[-1] == ("Bearer r", {"token": "t"})
        assert await d._post(f"{url}/broken", {}, {}) == (502, None)
    finally:
        if d.session is not None:
            await d.session.close()
        await server.close()


async def test_built_in_parts_are_installed_at_boot_once_and_a_bad_one_is_logged(tmp_path, short_dir, caplog):
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    data, sha, sig = part_archive(tmp_path / "p", name="tools", version="3.10.0", arch="any")
    (builtin / "tools-3.10.0-any.tar.gz").write_bytes(data)
    (builtin / "tools-3.10.0-any.sha256").write_text(sha + "\n")
    (builtin / "tools-3.10.0-any.sig").write_text(sig + "\n")
    (builtin / "zeta-1.0.0-any.tar.gz").write_bytes(b"not a part")  # no .sha256 beside it
    c = Config(
        tmp_path / "data",
        "hosted",
        "http://127.0.0.1:1",
        0,
        0,
        None,
        Path(rrotor.__file__).parent,
        short_dir,
        release_key=keys().public_text,
        builtin_parts=builtin,
    )
    d = Daemon(c, ready(), launcher=LocalLauncher())
    with caplog.at_level("WARNING", logger="rrotor"):
        assert d.install_builtin() == ["tools-3.10.0-any"]
    assert "a built-in part was not installed" in caplog.text
    assert [i.info.version for i in d.parts.installed()] == ["3.10.0"]
    assert d.install_builtin() == []  # already there at this version
    none = dataclasses.replace(c, builtin_parts=tmp_path / "nowhere")
    assert Daemon(none, ready(), launcher=LocalLauncher()).install_builtin() == []
    stop = asyncio.Event()
    stop.set()
    with caplog.at_level("INFO", logger="rrotor"):
        await Daemon(dataclasses.replace(c, data=tmp_path / "data2"), ready(), launcher=LocalLauncher()).serve(stop)
    assert "built-in part installed" in caplog.text


TOKEN = "tok_" + "b" * 20


def hosted_daemon(tmp_path: Path, short_dir: Path, runtime_token: str | None = TOKEN) -> Daemon:
    c = Config(
        tmp_path / "data",
        "hosted",
        "http://127.0.0.1:1",
        0,
        0,
        None,
        Path(rrotor.__file__).parent,
        short_dir,
        runtime_id=str(uuid.uuid4()) if runtime_token else None,
        runtime_token=runtime_token,
        machine_id="3d8f1a2b3c4d5e",
    )
    d = Daemon(c, ready(), launcher=LocalLauncher())
    if runtime_token:
        d.link.outbox.restart(digest(runtime_token))  # as the link does when it first runs
    return d


async def test_usage_is_owed_as_one_numbered_message_and_the_counts_start_again(tmp_path, short_dir):
    d = hosted_daemon(tmp_path, short_dir)
    await d.report_usage()
    assert d.link.outbox.pending == [] and d.link.outbox.seq == 0  # idle: nothing owed
    d.access.usage.count("a" * 64)
    d.access.usage.count("b" * 64)
    d.access.usage.count("a" * 64)
    await d.report_usage()
    [owed] = d.link.outbox.pending
    assert owed == {
        "type": "usage",
        "id": owed["id"],
        "seq": 1,
        "keys": [
            {"key_sha256": "a" * 64, "calls": 2, "last_used_at": owed["keys"][0]["last_used_at"]},
            {"key_sha256": "b" * 64, "calls": 1, "last_used_at": owed["keys"][1]["last_used_at"]},
        ],
    }
    assert d.access.usage.counts == {}
    await d.report_usage()
    assert len(d.link.outbox.pending) == 1 and d.link.outbox.seq == 1


async def test_no_usage_is_owed_without_a_runtime_token(tmp_path, short_dir):
    d = hosted_daemon(tmp_path, short_dir, runtime_token=None)
    d.access.usage.count("a" * 64)
    await d.report_usage()
    assert d.link.outbox.pending == [] and d.access.usage.counts == {}


async def test_usage_is_reported_every_minute_and_survives_a_failure(tmp_path, short_dir, monkeypatch, caplog):
    monkeypatch.setattr(daemon, "USAGE_EVERY", 0.01)
    d = hosted_daemon(tmp_path, short_dir)
    calls = []

    def take() -> list[dict[str, Any]]:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk full")
        return []

    monkeypatch.setattr(d.access.usage, "take", take)
    task = asyncio.create_task(d._usage())
    for _ in range(200):
        if len(calls) >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(calls) >= 2 and "reporting usage failed" in caplog.text


async def test_usage_is_flushed_once_more_on_shutdown(tmp_path, short_dir):
    d = hosted_daemon(tmp_path, short_dir)
    d.access.usage.count("a" * 64)
    stop = asyncio.Event()
    stop.set()
    await d.serve(stop)
    assert [(m["type"], m["seq"], m["keys"][0]["calls"]) for m in d.link.outbox.pending] == [("usage", 1, 1)]


async def test_a_failed_flush_on_shutdown_is_logged_and_it_still_stops(tmp_path, short_dir, monkeypatch, caplog):
    d = hosted_daemon(tmp_path, short_dir)

    def take() -> list[dict[str, Any]]:
        raise OSError("disk full")

    monkeypatch.setattr(d.access.usage, "take", take)
    stop = asyncio.Event()
    stop.set()
    await d.serve(stop)
    assert "reporting usage failed" in caplog.text


def self_daemon(tmp_path: Path, short_dir: Path, link: bool = True) -> Daemon:
    c = Config(
        tmp_path / "data",
        "self",
        "http://127.0.0.1:1",
        0,
        0,
        None,
        Path(rrotor.__file__).parent,
        short_dir,
        runtime_id=str(uuid.uuid4()),
        runtime_token=TOKEN,
        public_host="rrotor.acme.com",
        link=link,
        node_id="rrotor-1",
    )
    return Daemon(c, ready(), launcher=LocalLauncher())


async def test_a_self_hosted_daemon_links_and_introspects_unless_its_link_is_off(tmp_path, short_dir):
    on = self_daemon(tmp_path / "on", short_dir)
    assert on.link.configured and on.link.state == "no_token" and on.services().introspect is not None
    browser, catalogue = on.services().browser, on.services().catalogue
    assert browser is not None and browser.address == "https://rrotor.acme.com"
    assert catalogue is not None and catalogue.linked
    assert browser.redirect_uri == "https://rrotor.acme.com/auth/callback" and on.handler.catalogue is on.catalogue
    assert self_daemon(tmp_path / "off2", short_dir, link=False).services().browser is None
    assert Daemon(config(tmp_path, short_dir), ready()).services().browser is None  # local: no sign-in to make
    assert on.heartbeat is not None and on.services().heartbeat is on.heartbeat
    off = self_daemon(tmp_path / "off", short_dir, link=False)
    assert off.link.credentials.token == TOKEN and not off.link.configured
    assert off.link.view()["state"] == "off" and off.services().introspect is None
    assert off.heartbeat is None and hosted_daemon(tmp_path / "hosted", short_dir).heartbeat is None
    await off.link.run()  # returns at once: nothing to connect
    off.access.usage.count("a" * 64)
    stop = asyncio.Event()
    stop.set()
    await off.serve(stop)
    assert off.link.outbox.pending == []  # nothing owed to a Cloud it never reaches


@pytest.mark.parametrize(
    ("given", "state", "tier", "nodes"),
    [
        (None, "none", "free", 1),
        ("not-a-key", "bad", "free", 1),
        (30, "live", "pro", 3),
        (-2, "grace", "pro", 3),
        (-20, "expired", "pro", 1),
    ],
)
async def test_a_node_runs_on_no_key_a_bad_key_a_good_key_and_an_expired_one(
    tmp_path, short_dir, given, state, tier, nodes
):
    c = self_daemon(tmp_path / "first", short_dir, link=False).config
    if isinstance(given, int):
        expires = (datetime.now(UTC) + timedelta(days=given)).isoformat().replace("+00:00", "Z")
        given = keys().key(issued_at="2026-01-01T00:00:00Z", expires_at=expires)
    if given is not None:
        c = dataclasses.replace(c, licence_env=(("GLYPHH_LICENSE_KEY", given),))
    d = Daemon(dataclasses.replace(c, data=tmp_path / "data"), ready(), launcher=LocalLauncher())
    view = d.services().licence.view()
    assert (view["state"], view["tier"], view["max_nodes"]) == (state, tier, nodes)
    stop = asyncio.Event()
    stop.set()
    await d.serve(stop)  # serves, and stops, whatever the key


def test_the_environments_key_and_a_legacy_offline_licence_are_read_at_boot(tmp_path, short_dir):
    d = self_daemon(tmp_path, short_dir, link=False)
    runtime_id = d.config.runtime_id
    offline = keys().licence(["rrotor"], expires="2998-01-01T00:00:00Z", grace_days=14, kind="offline", plan="Team")
    own = keys().licence(["rrotor"], runtime_id=runtime_id, kind="offline", licence_id="own")
    c = dataclasses.replace(d.config, data=tmp_path / "booted")
    booted = Daemon(
        dataclasses.replace(c, licence_env=(("RROTOR_LICENCE", keys().token(offline)),)),
        ready(),
        launcher=LocalLauncher(),
    )
    assert booted.licence.limits().tier == "team" and booted.licence.view()["source"] == "RROTOR_LICENCE"
    again = Daemon(
        dataclasses.replace(c, data=tmp_path / "again", licence_env=(("RROTOR_LICENCE", keys().token(own)),)),
        ready(),
        launcher=LocalLauncher(),
    )
    assert again.licence.licence_id == "own" and again.licence.runtime_id == runtime_id
    both = (("GLYPHH_LICENSE_KEY", keys().key(tier="team", max_nodes=10)), ("RROTOR_LICENCE", keys().token(offline)))
    keyed = Daemon(dataclasses.replace(c, data=tmp_path / "keyed", licence_env=both), ready(), launcher=LocalLauncher())
    assert keyed.licence.view()["source"] == "GLYPHH_LICENSE_KEY"  # the key preferred to the legacy licence


async def test_the_key_file_is_read_again_every_hour_and_a_failure_is_logged(tmp_path, short_dir, monkeypatch, caplog):
    monkeypatch.setattr(daemon, "LICENCE_EVERY", 0.01)
    d = self_daemon(tmp_path, short_dir, link=False)
    ticks = []

    def tick() -> None:
        ticks.append(1)
        if len(ticks) == 1:
            raise OSError("disk gone")

    monkeypatch.setattr(d.licence, "tick", tick)
    task = asyncio.create_task(d._licence())
    for _ in range(200):
        if len(ticks) >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(ticks) >= 2 and "reading the licence key again failed" in caplog.text


async def test_sighup_reads_the_key_file_again_while_serving(tmp_path, short_dir, caplog):
    d = self_daemon(tmp_path, short_dir, link=False)
    assert d.licence.state == "none"
    stop = asyncio.Event()
    serving = asyncio.create_task(d.serve(stop))
    for _ in range(200):
        if signal.getsignal(signal.SIGHUP) != signal.SIG_DFL:
            break
        await asyncio.sleep(0.01)
    (d.config.data / "licence.token").write_text(keys().key(tier="team", max_nodes=10))
    os.kill(os.getpid(), signal.SIGHUP)
    for _ in range(100):
        if d.licence.state == "live":
            break
        await asyncio.sleep(0.01)
    assert d.licence.limits().tier == "team"

    def broken() -> None:
        raise OSError("disk gone")

    d.licence.tick = broken  # type: ignore[method-assign]
    os.kill(os.getpid(), signal.SIGHUP)
    for _ in range(100):
        if "reading the licence key again failed" in caplog.text:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(serving, 5)
    assert "reading the licence key again failed" in caplog.text
    assert signal.getsignal(signal.SIGHUP) == signal.SIG_DFL  # the handler goes with the daemon


async def test_a_self_hosted_daemon_starts_its_heartbeat_with_the_link(tmp_path, short_dir):
    d = self_daemon(tmp_path, short_dir)
    started = []
    stop = asyncio.Event()

    async def run() -> None:
        started.append(1)
        stop.set()

    d.heartbeat.run = run  # type: ignore[method-assign,union-attr]
    await d.serve(stop)
    assert started == [1]


def test_a_self_hosted_daemon_whose_bwrap_cannot_start_says_what_the_container_lacks(tmp_path, short_dir, caplog):
    failed = Layers({"bubblewrap": {"ok": False, "detail": "bwrap cannot make its namespaces here"}})
    c = self_daemon(tmp_path, short_dir).config
    with caplog.at_level("WARNING", logger="rrotor"):
        d = Daemon(c, failed, launcher=LocalLauncher())
    assert d.layers.needs and d.services().layers.to_json()["container_needs"] == d.layers.needs
    assert "the container lacks" in caplog.text
    hosted = Layers({"bubblewrap": {"ok": False, "detail": "x"}})
    Daemon(dataclasses.replace(c, mode="hosted", data=tmp_path / "h"), hosted, launcher=LocalLauncher())
    assert hosted.needs == [] and "container_needs" not in hosted.to_json()
    assert Daemon(dataclasses.replace(c, data=tmp_path / "ok"), ready(), launcher=LocalLauncher()).layers.needs == []


def enterprise() -> str:
    return keys().key(tier="enterprise", max_nodes=None, max_precedents=None, issued_at="2026-10-01T00:00:00Z")


async def test_an_enterprise_node_keeps_its_audit_events_naming_who_did_each(tmp_path, short_dir):
    c = dataclasses.replace(
        self_daemon(tmp_path / "first", short_dir).config,
        data=tmp_path / "data",
        licence_env=(("GLYPHH_LICENSE_KEY", enterprise()),),
    )
    d = Daemon(c, ready(), launcher=LocalLauncher())
    assert d.audit.kept and d.services().audit is d.audit and d.runner.audit is d.audit and d.handler.audit is d.audit
    d.access.set_keys(["c" * 64])
    d.licence.offer("junk.junk", "heartbeat")
    kinds = [(e["kind"], e["actor"]["type"]) for e in d.audit.read(None, 100)[0]]
    assert kinds == [("licence.changed", "rrotor"), ("keys.changed", "server"), ("licence.bad", "server")]
    assert d.drain is None and d.runner.telemetry is None and d.services().drain is None


def drained(tmp_path: Path, short_dir: Path, url: str, **kw: Any) -> Daemon:
    given: dict[str, Any] = {"data": tmp_path / "data", "server": url, "otel": (("OTEL_EXPORTER_OTLP_ENDPOINT", url),)}
    c = dataclasses.replace(self_daemon(tmp_path / "first", short_dir).config, **(given | kw))
    return Daemon(c, ready(), launcher=LocalLauncher())


async def test_a_drain_is_wired_with_its_gauges_and_flushed_at_stop(tmp_path, short_dir, monkeypatch):
    from tests.fakes import FakeCollector

    fake = FakeCollector()
    url = await fake.start()
    root = logging.getLogger()
    level, handlers = root.level, root.handlers[:]
    root.setLevel(logging.INFO)
    try:
        d = drained(tmp_path, short_dir, url, licence_env=(("GLYPHH_LICENSE_KEY", keys().key()),))
        assert d.drain is not None and d.runner.telemetry is d.drain.telemetry and d.services().drain is d.drain
        stop = asyncio.Event()
        stop.set()
        await d.serve(stop)
        assert d.drain.handler not in root.handlers
    finally:
        root.setLevel(level)
        root.handlers[:] = handlers
        await fake.close()
    lines = [r["body"] for r in fake.logs()]
    assert "rrotor up" in lines and "rrotor stopped" in lines
    assert all(r["node_id"] == "rrotor-1" for r in fake.logs())
    assert "rrotor.link.state" in fake.metrics()
    standalone = drained(tmp_path / "e", short_dir, url, licence_env=(("GLYPHH_LICENSE_KEY", enterprise()),))
    assert standalone.drain is not None
    data = standalone.drain.telemetry.collect()
    assert data is not None
    names = {m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
    assert "rrotor.cluster.term" not in names and "rrotor.link.state" in names  # no cluster: no term to show
    standalone.drain.telemetry.shutdown()


async def test_server_side_logging_is_said_at_every_start_and_turning_it_on_or_off_is_audited(
    tmp_path, short_dir, caplog
):
    on = drained(tmp_path, short_dir, "http://127.0.0.1:1", server_logs=True, otel=())
    assert on.drain is not None and on.drain.glyphh is not None and on.drain.collector is None
    on.audit.tier = lambda: "enterprise"
    with caplog.at_level(logging.WARNING, logger="rrotor"):
        on.server_logs()
        on.server_logs()  # still on: said again, not audited again
    said = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Server-side logging is on")]
    assert len(said) == 2 and "(127.0.0.1)" in said[0]
    assert [(e["kind"], e["on"]) for e in on.audit.read(None, 10)[0]] == [("config.server_logs", True)]
    on.config = dataclasses.replace(on.config, server_logs=False)
    on.server_logs()
    assert [e["on"] for e in on.audit.read(None, 10)[0]] == [True, False]
    health = on.services()
    assert health.drain is on.drain
    await on.drain.stop()
