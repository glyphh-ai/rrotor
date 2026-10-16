"""The browser part's state and its routes (its frame protocol), with fake profiles."""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from rrotord.api.errors import errors
from rrotord.browser import part
from rrotord.browser.cdp import CdpError
from rrotord.browser.chrome import Page
from rrotord.browser.part import Browser, fetch_limited
from rrotord.config import Config
from rrotord.errors import BadRequest, Unavailable


class FakeProfile:
    fail: BaseException | None = None

    def __init__(self, *, name: str, folder: Path, chromium: str, on_frame: Any, emit: Any) -> None:
        self.name, self.folder, self.on_frame, self.emit = name, folder, on_frame, emit
        self.pages: dict[str, Page] = {}
        self.ops: list[dict[str, Any]] = []
        self.started_with: dict[str, str] | None = None
        self.stopped = False
        self.running = False

    async def start(self, env: dict[str, str]) -> None:
        if FakeProfile.fail is not None:
            raise FakeProfile.fail
        self.started_with, self.running = env, True

    async def op(self, cmd: dict[str, Any]) -> None:
        self.ops.append(cmd)
        if cmd.get("op") == "open":
            self.pages[cmd["cell"]] = Page(cmd["cell"], "t", "s", url=cmd.get("url", ""), title="T")
        if cmd.get("op") == "boom":
            raise CdpError("detached")
        if cmd.get("op") == "slow":
            await asyncio.sleep(10)

    async def text(self, cell: str) -> dict[str, Any] | None:
        return {"cell": cell, "text": "hello"} if cell in self.pages else None

    async def stop(self) -> None:
        self.stopped, self.running = True, False


@pytest.fixture(autouse=True)
def no_failure():
    FakeProfile.fail = None
    yield
    FakeProfile.fail = None


def make(tmp_path: Path, fetch: Any = None) -> Browser:
    async def no_fetch(url: str, limit: int) -> None:
        return None

    return Browser("/usr/bin/chromium", tmp_path / "profiles", tmp_path / "home", fetch or no_fetch, FakeProfile)  # type: ignore[arg-type]


async def until_listening(b: Browser) -> None:
    for _ in range(500):
        if b.events:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("nobody listens")


async def gone(group: set[Any]) -> None:
    for _ in range(50):
        if not group:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("the stream stayed")


async def client_for(browser: Browser) -> TestClient[web.Request, web.Application]:
    app = web.Application(middlewares=[errors])
    browser.routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def config(tmp_path: Path) -> Config:
    return Config(tmp_path / "data", "local", "http://s", 7777, 7777, None, tmp_path, tmp_path / "run")


def test_find_takes_the_environment_first(tmp_path):
    found = Browser.find(config(tmp_path), {"RROTOR_CHROMIUM": "/opt/chromium"})
    assert found is not None and found.chromium == "/opt/chromium"
    assert (found.root, found.home) == (tmp_path / "data" / "profiles", tmp_path / "run" / "chromium-home")


def test_find_looks_where_debian_puts_chromium(tmp_path, monkeypatch):
    binary = tmp_path / "chromium"
    binary.write_text("")
    monkeypatch.setattr(part, "CHROMIUM", (str(tmp_path / "none"), str(binary)))
    assert Browser.find(config(tmp_path), {}).chromium == str(binary)  # type: ignore[union-attr]


def test_find_without_chromium(tmp_path, monkeypatch):
    monkeypatch.setattr(part, "CHROMIUM", (str(tmp_path / "none"),))
    monkeypatch.delenv("RROTOR_CHROMIUM", raising=False)
    assert Browser.find(config(tmp_path)) is None


async def test_ops_open_a_page_in_its_profile(tmp_path):
    b = make(tmp_path)
    await b.op({"op": "open", "cell": "c1", "url": "https://a/", "profile": "work"})
    await b.op({"op": "reload", "cell": "c1"})
    await b.op({"op": "reload", "cell": "unknown"})
    profile = b.profiles["work"]
    assert [o["op"] for o in profile.ops] == ["open", "reload"]  # type: ignore[attr-defined]
    assert b.cells == {"c1": "work"} and (tmp_path / "home").is_dir()
    assert profile.started_with["HOME"] == str(tmp_path / "home")  # type: ignore[attr-defined]
    assert b.view() == {
        "ready": True,
        "detail": None,
        "chromium": "/usr/bin/chromium",
        "profiles": ["work"],
        "pages": 1,
    }
    await b.op({"op": "open", "cell": "c1"})
    assert b.cells["c1"] == "work"
    b.last["c1"] = (b"x", 1.0)
    await b.op({"op": "close", "cell": "c1"})
    assert b.cells == {} and b.last == {}


async def test_a_cell_opened_without_a_profile_uses_default(tmp_path):
    b = make(tmp_path)
    await b.op({"op": "open", "cell": "c1"})
    assert b.cells == {"c1": "default"}


async def test_a_profile_name_must_be_lowercase(tmp_path):
    with pytest.raises(BadRequest) as caught:
        await make(tmp_path).profile("Bad Name")
    assert caught.value.code == "E_PROFILE"


async def test_a_chromium_that_does_not_start_is_unavailable_and_shown(tmp_path):
    b = make(tmp_path)
    FakeProfile.fail = OSError("no sandbox")
    with pytest.raises(Unavailable) as caught:
        await b.profile("default")
    assert caught.value.code == "E_BROWSER" and b.view()["ready"] is False
    assert b.view()["detail"] == "Chromium did not start: no sandbox"
    FakeProfile.fail = None
    await b.profile("default")
    assert b.error is None


async def test_a_stopped_profile_is_started_again(tmp_path):
    b = make(tmp_path)
    first = await b.profile("default")
    assert await b.profile("default") is first
    first.running = False  # type: ignore[misc]
    assert await b.profile("default") is not first


async def test_reap_stops_idle_profiles_without_pages(tmp_path):
    b = make(tmp_path)
    await b.op({"op": "open", "cell": "c1", "profile": "busy"})
    idle = await b.profile("idle")
    b.used["idle"] = 0.0
    assert await b.reap(now=part.IDLE + 1) == ["idle"]
    assert idle.stopped and "busy" in b.profiles  # type: ignore[attr-defined]
    assert await b.reap() == []


async def test_run_reaps_every_minute(tmp_path, monkeypatch):
    b = make(tmp_path)
    reaped = []

    async def reap(now: float | None = None) -> list[str]:
        reaped.append(now)
        return []

    sleeps = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(b, "reap", reap)
    monkeypatch.setattr(part, "asyncio", types.SimpleNamespace(sleep=sleep))
    with pytest.raises(asyncio.CancelledError):
        await b.run()
    assert sleeps == [60, 60] and reaped == [None]


async def test_shutdown_ends_streams_and_stops_profiles(tmp_path):
    b = make(tmp_path)
    profile = await b.profile("default")

    class Res:
        def __init__(self, fail: bool) -> None:
            self.fail, self.ended = fail, False

        async def write_eof(self) -> None:
            if self.fail:
                raise ConnectionResetError
            self.ended = True

    good, bad = Res(False), Res(True)
    b.streams.add(good)  # type: ignore[arg-type]
    b.events.add(bad)  # type: ignore[arg-type]
    await b.shutdown()
    assert good.ended and profile.stopped and b.profiles == {}  # type: ignore[attr-defined]


async def test_a_write_to_a_gone_client_drops_it(tmp_path):
    class Res:
        async def write(self, data: bytes) -> None:
            raise ConnectionResetError

    res = Res()
    group = {res}
    Browser._write(res, b"x", group)  # type: ignore[arg-type]
    await asyncio.gather(*list(part._WRITES))
    assert group == set()


async def read_until(stream: Any, needle: bytes, seconds: float = 5.0) -> bytes:
    data = b""
    while needle not in data:
        data += await asyncio.wait_for(stream.content.readany(), seconds)
    return data


async def test_frames_stream_the_newest_frame_first_then_live_ones(tmp_path):
    b = make(tmp_path)
    b._frame("c1", b"old", 1.0)
    b._frame("c1", b"newest", 2.0)
    client = await client_for(b)
    try:
        res = await client.get("/frames")
        assert res.headers["Content-Type"] == "multipart/mixed; boundary=glyphhframe"
        assert res.headers["X-Content-Type-Options"] == "nosniff"
        first = await read_until(res, b"newest")
        assert b"X-Cell: c1\r\nX-Captured: 2\r\nContent-Length: 6\r\n\r\nnewest" in first and b"old" not in first
        b._frame("c 1#popup", b"live", 3.0)
        assert b"X-Cell: c%201%23popup" in await read_until(res, b"live")
        res.close()
        await gone(b.streams)
    finally:
        await client.close()


async def test_events_say_hello_then_the_pages_events(tmp_path):
    b = make(tmp_path)
    b.cells["c1"] = "work"
    client = await client_for(b)
    try:
        res = await client.get("/events")
        assert res.headers["Content-Type"] == "text/event-stream"
        assert await read_until(res, b": hello\n\n")
        await until_listening(b)
        b._emit({"t": "popup", "cell": "c1", "popup": "c1#popup", "url": "https://p/"})
        data = await read_until(res, b"popup")
        assert json.loads(data.split(b"data: ")[1]) == {
            "t": "popup",
            "cell": "c1",
            "popup": "c1#popup",
            "url": "https://p/",
        }
        assert b.cells["c1#popup"] == "work"
        b.last["c1#popup"] = (b"x", 1.0)
        b._emit({"t": "popup-closed", "cell": "c1", "popup": "c1#popup"})
        b._emit({"t": "nav", "cell": "c1", "url": "https://n/"})
        await read_until(res, b"https://n/")
        assert "c1#popup" not in b.cells and "c1#popup" not in b.last
        res.close()
        await gone(b.events)
    finally:
        await client.close()


async def test_a_popup_of_an_unknown_cell_uses_default(tmp_path):
    b = make(tmp_path)
    b._emit({"t": "popup", "cell": "x", "popup": "x#popup"})
    assert b.cells["x#popup"] == "default"


async def test_ops_route_applies_one_or_many_in_order(tmp_path, monkeypatch):
    monkeypatch.setattr(part, "OP_WAIT", 0.05)
    b = make(tmp_path)
    client = await client_for(b)
    try:
        res = await client.post("/api/ops", json={"op": "open", "cell": "c1"})
        assert res.status == 200 and await res.json() == {}
        ops = [{"op": "boom", "cell": "c1"}, "skipped", {"op": "slow", "cell": "c1"}, {"op": "reload", "cell": "c1"}]
        assert (await client.post("/api/ops", json=ops)).status == 200
        assert [o["op"] for o in b.profiles["default"].ops] == ["open", "boom", "slow", "reload"]  # type: ignore[attr-defined]
        res = await client.post("/api/ops", data=b"{")
        assert res.status == 400
    finally:
        await client.close()


async def test_text_and_pages_routes(tmp_path):
    b = make(tmp_path)
    await b.op({"op": "open", "cell": "c1", "url": "https://a/", "profile": "work"})
    client = await client_for(b)
    try:
        assert await (await client.get("/api/text?cell=c1")).json() == {"cell": "c1", "text": "hello"}
        assert await (await client.get("/api/text?cell=none")).json() == {"error": "no such page"}
        b.cells["ghost"] = "work"
        assert await (await client.get("/api/text?cell=ghost")).json() == {"error": "no such page"}
        pages = await (await client.get("/api/browser")).json()
        assert pages == {"pages": [{"cell": "c1", "url": "https://a/", "title": "T", "profile": "work"}]}
    finally:
        await client.close()


async def test_favicon_finds_the_best_icon_and_keeps_it(tmp_path):
    asked: list[str] = []
    answers = {
        "https://a.example.com/p": (
            "text/html",
            b'<link rel="icon" sizes="32" href="/i.png"><link rel="icon" href="/bad.png">',
        ),
        "https://a.example.com/i.png": ("text/html", b"not an image"),
        "https://a.example.com/bad.png": None,
        "https://a.example.com/favicon.ico": ("image/x-icon", b"ICO"),
    }

    async def fetch(url: str, limit: int) -> tuple[str, bytes] | None:
        asked.append(url)
        return answers.get(url)

    b = make(tmp_path, fetch)
    client = await client_for(b)
    try:
        res = await client.get("/api/favicon?url=https://a.example.com/p")
        assert (res.status, await res.read(), res.headers["Content-Type"]) == (200, b"ICO", "image/x-icon")
        assert res.headers["Cache-Control"] == "private, max-age=86400"
        count = len(asked)
        assert (await client.get("/api/favicon?url=https://a.example.com/other")).status == 200
        assert len(asked) == count
    finally:
        await client.close()


async def test_favicon_not_found(tmp_path):
    b = make(tmp_path)
    client = await client_for(b)
    try:
        assert (await client.get("/api/favicon?url=javascript:x")).status == 404
        assert (await client.get("/api/favicon")).status == 404
        assert (await client.get("/api/favicon?url=https://none.example.com/")).status == 404
        assert b.favicons == {"https://none.example.com": None}
    finally:
        await client.close()


async def test_fetch_limited_with_a_time_and_size_limit():
    async def ok(_: web.Request) -> web.Response:
        return web.Response(body=b"12345", content_type="image/png")

    async def missing(_: web.Request) -> web.Response:
        return web.Response(status=404)

    app = web.Application()
    app.router.add_get("/ok", ok)
    app.router.add_get("/missing", missing)
    server = TestServer(app)
    await server.start_server()
    try:
        assert await fetch_limited(str(server.make_url("/ok")), 10) == ("image/png", b"12345")
        assert await fetch_limited(str(server.make_url("/ok")), 4) is None
        assert await fetch_limited(str(server.make_url("/missing")), 10) is None
        assert await fetch_limited("http://127.0.0.1:1/", 10) is None
    finally:
        await server.close()


async def test_a_stream_whose_client_is_gone_ends(tmp_path, monkeypatch):
    b = make(tmp_path)
    b._frame("c1", b"frame", 1.0)

    async def hold(request: Any, res: Any, group: set[Any]) -> None:
        await asyncio.gather(*list(part._WRITES))
        group.discard(res)

    monkeypatch.setattr(b, "_hold", hold)
    client = await client_for(b)
    try:
        assert b"frame" in await (await client.get("/frames")).read()
        assert await (await client.get("/events")).read() == b": hello\n\n"
    finally:
        await client.close()


async def test_hold_returns_at_once_for_a_closed_connection():
    group: set[Any] = {"res"}
    closed = types.SimpleNamespace(transport=types.SimpleNamespace(is_closing=lambda: True))
    await Browser._hold(closed, "res", group)  # type: ignore[arg-type]
    group.add("res")
    await Browser._hold(types.SimpleNamespace(transport=None), "res", group)  # type: ignore[arg-type]
    assert group == set()
