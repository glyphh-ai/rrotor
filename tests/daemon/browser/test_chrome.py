"""A profile's pages against a fake CDP that answers each method as the test says."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import pytest

from rrotord.browser import rules
from rrotord.browser.cdp import CdpError
from rrotord.browser.chrome import Page, Profile, popup_cell


class FakeCdp:
    def __init__(self, on_event: Any, answers: dict[str, Any]) -> None:
        self.on_event, self.answers = on_event, answers
        self.calls: list[tuple[str, dict[str, Any] | None, str | None]] = []
        self.closed = asyncio.Event()
        self.started: tuple[str, list[str], dict[str, str]] | None = None
        self.stopped = False
        self.targets = 0

    async def start(self, chromium: str, args: list[str], env: dict[str, str]) -> None:
        self.started = (chromium, args, env)

    def send(
        self, method: str, params: dict[str, Any] | None = None, session: str | None = None
    ) -> asyncio.Future[dict[str, Any]]:
        self.calls.append((method, params, session))
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        answer = self.answers.get(method, {})
        if method == "Target.createTarget" and method not in self.answers:
            self.targets += 1
            answer = {"targetId": f"t{self.targets}"}
        if method == "Target.attachToTarget" and method not in self.answers:
            answer = {"sessionId": f"s-{(params or {}).get('targetId')}"}
        if isinstance(answer, BaseException):
            future.set_exception(answer)
        else:
            future.set_result(answer)
        return future

    async def stop(self) -> None:
        self.stopped = True

    def methods(self) -> list[str]:
        return [c[0] for c in self.calls]


DEFAULTS: dict[str, Any] = {
    "Browser.getVersion": {"userAgent": "Mozilla/5.0 (X11; Linux) HeadlessChrome/120.0 Safari"},
    "Page.setWebLifecycleState": CdpError("old chromium"),
    "Page.createIsolatedWorld": {"executionContextId": 5},
    "Runtime.evaluate": {"result": {"value": "Title"}},
}


class Harness:
    def __init__(self, tmp_path: Path, **answers: Any) -> None:
        self.frames: list[tuple[str, bytes, float]] = []
        self.events: list[dict[str, Any]] = []
        self.answers = {**DEFAULTS, **answers}
        self.cdp: FakeCdp | None = None

        def factory(on_event: Any) -> FakeCdp:
            self.cdp = FakeCdp(on_event, self.answers)
            return self.cdp

        self.profile = Profile(
            name="default",
            folder=tmp_path / "profiles" / "default",
            chromium="/usr/bin/chromium",
            on_frame=lambda c, d, t: self.frames.append((c, d, t)),
            emit=self.events.append,
            factory=factory,  # type: ignore[arg-type]
        )

    async def start(self) -> Harness:
        await self.profile.start({"HOME": "/tmp"})
        return self

    @property
    def fake(self) -> FakeCdp:
        assert self.cdp is not None
        return self.cdp

    async def settle(self) -> None:
        for _ in range(5):
            await asyncio.gather(*list(self.profile.tasks), return_exceptions=True)
            await asyncio.sleep(0)

    def message(self, method: str, session: str | None = None, **params: Any) -> None:
        msg: dict[str, Any] = {"method": method, "params": params}
        if session is not None:
            msg["sessionId"] = session
        self.profile.on_message(msg)


@pytest.fixture
async def h(tmp_path):
    return await Harness(tmp_path).start()


async def test_start_runs_chromium_headless_on_a_pipe(h, tmp_path):
    chromium, args, env = h.fake.started
    assert chromium == "/usr/bin/chromium" and "--remote-debugging-pipe" in args
    assert f"--user-data-dir={tmp_path / 'profiles' / 'default'}" in args and env == {"HOME": "/tmp"}
    assert (tmp_path / "profiles" / "default").is_dir()
    assert h.fake.methods() == ["Target.setDiscoverTargets"] and h.profile.running
    h.fake.closed.set()
    assert not h.profile.running


async def test_open_makes_a_prepared_casting_page(h):
    page = await h.profile.open(
        {"cell": "c1", "url": "https://a.example.com/", "w": 640, "h": 480, "scale": 2, "keepInPanel": True}
    )
    assert (page.target, page.session, page.keep, page.w, page.h, page.scale) == ("t1", "s-t1", True, 640, 480, 2.0)
    methods = h.fake.methods()
    for expected in ("Page.enable", "Network.setUserAgentOverride", "Runtime.addBinding", "Page.startScreencast"):
        assert expected in methods
    assert methods[-1] == "Page.navigate"
    metrics = next(c[1] for c in h.fake.calls if c[0] == "Emulation.setDeviceMetricsOverride")
    assert metrics == {"width": 320, "height": 240, "deviceScaleFactor": 2.0, "mobile": False}
    ua = next(c[1] for c in h.fake.calls if c[0] == "Network.setUserAgentOverride")
    assert ua["userAgent"] == "Mozilla/5.0 (X11; Linux) Chrome/140.0.0.0 Safari"
    await h.profile.open({"cell": "c2"})
    assert h.fake.methods().count("Browser.getVersion") == 1
    assert h.profile.pages["c2"].url == "about:blank" and (h.profile.pages["c2"].w, h.profile.pages["c2"].h) == (
        800,
        600,
    )


async def test_open_without_an_isolated_world_still_works(tmp_path):
    h = await Harness(tmp_path, **{"Page.createIsolatedWorld": {}}).start()
    await h.profile.open({"cell": "c1"})
    assert "Page.navigate" in h.fake.methods()


async def test_open_an_existing_page(h):
    await h.profile.open({"cell": "c1", "w": 640, "h": 480})
    h.fake.calls.clear()
    same = await h.profile.open({"cell": "c1", "w": 640, "h": 480})
    assert h.fake.calls == [] and same is h.profile.pages["c1"]
    await h.profile.open({"cell": "c1", "w": 700, "h": 480})
    assert "Emulation.setDeviceMetricsOverride" in h.fake.methods()
    h.profile.pages["c1"].live = False
    h.fake.calls.clear()
    await h.profile.open({"cell": "c1", "w": 700, "h": 480})
    assert h.fake.methods() == ["Page.stopScreencast", "Page.startScreencast"]


async def test_sizes_have_a_floor(h):
    page = await h.profile.open({"cell": "c1", "w": 10, "h": 10})
    assert (page.w, page.h) == (64, 48)


async def opened(h: Harness, keep: bool = False, url: str = "https://p.example.com/") -> Page:
    page = await h.profile.open({"cell": "c1", "keepInPanel": keep})
    page.url = url
    h.fake.calls.clear()
    return page


async def test_ops_on_a_page(h):
    page = await opened(h)
    s = page.session
    for cmd in (
        {"op": "resize", "w": 900},
        {"op": "navigate", "url": "example.com"},
        {"op": "back"},
        {"op": "reload"},
        {"op": "mouse", "action": "down", "x": 1, "y": 2},
        {"op": "wheel", "dy": 5},
        {"op": "key", "key": "a"},
        {"op": "text", "text": "hi"},
        {"op": "spin"},
        {"op": "hide"},
        {"op": "resize", "h": 700},
    ):
        await h.profile.op({"cell": "c1", **cmd})
    by = {c[0]: c for c in h.fake.calls}
    assert by["Page.navigate"][1] == {"url": "https://example.com"}
    assert {"expression": "history.back()"} in [c[1] for c in h.fake.calls]
    wheel = next(c for c in h.fake.calls if c[1] and c[1].get("type") == "mouseWheel")
    assert wheel[1] == {"type": "mouseWheel", "x": 450.0, "y": 300.0, "deltaX": 0, "deltaY": 5}
    assert all(c[2] == s for c in h.fake.calls)
    assert not page.live and (page.w, page.h) == (900, 700)
    assert h.fake.methods()[-1] == "Page.stopScreencast"


async def test_a_page_not_casting_yet_is_cast(tmp_path):
    h = await Harness(tmp_path, **{"Page.stopScreencast": CdpError("not casting")}).start()
    await h.profile.open({"cell": "c1"})
    assert "Page.startScreencast" in h.fake.methods()


async def test_op_on_a_cell_with_no_page_does_nothing(h):
    await h.profile.op({"op": "reload", "cell": "none"})
    assert h.fake.methods() == ["Target.setDiscoverTargets"]


async def test_op_open_opens(h):
    await h.profile.op({"op": "open", "cell": "c9"})
    assert "c9" in h.profile.pages


async def test_close_a_page(h):
    page = await opened(h)
    await h.profile.op({"op": "close", "cell": "c1"})
    assert h.profile.pages == {} and h.profile.sessions == {}
    assert h.fake.calls[-1] == ("Target.closeTarget", {"targetId": page.target}, None)


async def test_frames_are_acked_and_handed_on_while_live(h):
    page = await opened(h)
    data = base64.b64encode(b"jpeg").decode()
    h.message("Page.screencastFrame", page.session, data=data, sessionId=7, metadata={"timestamp": 1.5})
    await h.settle()
    assert h.frames == [("c1", b"jpeg", 1500.0)]
    assert ("Page.screencastFrameAck", {"sessionId": 7}, page.session) in h.fake.calls
    page.live = False
    h.message("Page.screencastFrame", page.session, data=data, sessionId=8)
    await h.settle()
    assert len(h.frames) == 1


async def test_navigation_and_title_events(h):
    page = await opened(h)
    h.message("Page.frameNavigated", page.session, frame={"url": "https://n.example.com/"})
    h.message("Page.frameNavigated", page.session, frame={"url": "https://iframe/", "parentId": "x"})
    h.message("Page.loadEventFired", page.session)
    h.message("Page.somethingElse", page.session)
    h.message("Page.loadEventFired", "unknown-session")
    await h.settle()
    assert h.events == [
        {"t": "nav", "cell": "c1", "url": "https://n.example.com/"},
        {"t": "title", "cell": "c1", "title": "Title"},
    ]
    assert page.title == "Title"


async def test_window_open_names_the_popups_address(h):
    page = await opened(h)
    h.message("Page.windowOpen", page.session, url="https://pop.example.com/")
    assert page.opening == "https://pop.example.com/"


async def test_a_popup_is_kept_over_its_cell(h):
    page = await opened(h)
    h.message(
        "Target.targetCreated", targetInfo={"type": "page", "openerId": page.target, "targetId": "pop", "url": ""}
    )
    page.opening = "https://pop.example.com/"
    await h.settle()
    popup = h.profile.pages[popup_cell("c1")]
    assert (popup.parent, popup.session) == ("c1", "s-pop")
    assert h.events[-1] == {"t": "popup", "cell": "c1", "popup": "c1#popup", "url": "https://pop.example.com/"}
    h.message(
        "Target.targetCreated",
        targetInfo={"type": "page", "openerId": page.target, "targetId": "pop2", "url": "https://two/"},
    )
    await h.settle()
    assert ("Target.closeTarget", {"targetId": "pop"}, None) in h.fake.calls
    assert h.profile.pages["c1#popup"].target == "pop2"
    h.message("Target.targetDestroyed", targetId="pop2")
    assert "c1#popup" not in h.profile.pages and h.events[-1] == {
        "t": "popup-closed",
        "cell": "c1",
        "popup": "c1#popup",
    }


async def test_closing_an_old_popup_that_is_gone_already(tmp_path):
    h = await Harness(tmp_path, **{"Target.closeTarget": CdpError("gone")}).start()
    page = await opened(h)
    for target in ("p1", "p2"):
        h.message(
            "Target.targetCreated",
            targetInfo={"type": "page", "openerId": page.target, "targetId": target, "url": "https://x/"},
        )
        await h.settle()
    assert h.profile.pages["c1#popup"].target == "p2"


async def test_closing_a_popup_by_op(h):
    page = await opened(h)
    h.message(
        "Target.targetCreated",
        targetInfo={"type": "page", "openerId": page.target, "targetId": "pop", "url": "https://x/"},
    )
    await h.settle()
    await h.profile.op({"op": "close", "cell": "c1#popup"})
    assert "c1#popup" not in h.profile.pages and h.events[-1]["t"] == "popup-closed"


async def test_a_popup_kept_in_panel_loads_in_the_page(h):
    page = await opened(h, keep=True)
    h.message(
        "Target.targetCreated",
        targetInfo={"type": "page", "openerId": page.target, "targetId": "pop", "url": "https://x.example.com/"},
    )
    await h.settle()
    assert ("Target.closeTarget", {"targetId": "pop"}, None) in h.fake.calls
    assert ("Page.navigate", {"url": "https://x.example.com/"}, page.session) in h.fake.calls


async def test_a_native_popup_is_denied(tmp_path):
    h = await Harness(tmp_path, **{"Target.closeTarget": CdpError("gone")}).start()
    page = await opened(h)
    h.message(
        "Target.targetCreated",
        targetInfo={"type": "page", "openerId": page.target, "targetId": "pop", "url": "slack://x"},
    )
    await h.settle()
    assert "Page.navigate" not in h.fake.methods() and "c1#popup" not in h.profile.pages


async def test_targets_that_are_not_popups_of_a_page_are_ignored(h):
    page = await opened(h)
    h.message("Target.targetCreated", targetInfo={"type": "worker", "openerId": page.target})
    h.message("Target.targetCreated", targetInfo={"type": "page"})
    h.message("Target.targetCreated", targetInfo={"type": "page", "openerId": "unknown", "targetId": "x"})
    h.message("Target.targetDestroyed", targetId=page.target)
    h.message("Target.other")
    await h.settle()
    assert h.fake.calls == [] and "c1" in h.profile.pages


async def test_a_native_link_clicked_kept_in_panel_goes_to_its_web_client(h):
    page = await opened(h, keep=True)
    h.message("Runtime.bindingCalled", page.session, name=rules.NATIVE_BINDING, payload="discord://x")
    h.message("Runtime.bindingCalled", page.session, name="other", payload="discord://x")
    await h.settle()
    assert h.fake.calls == [("Page.navigate", {"url": "https://discord.com/app"}, page.session)]


async def test_a_native_link_not_kept_or_a_web_link_is_ignored(h):
    page = await opened(h)
    h.message("Runtime.bindingCalled", page.session, name=rules.NATIVE_BINDING, payload="discord://x")
    h.message("Runtime.bindingCalled", page.session, name=rules.NATIVE_BINDING, payload="https://x/")
    await h.settle()
    assert h.fake.calls == []


async def test_a_native_navigation_renews_the_page(h):
    page = await opened(h, keep=True)
    old = page.session
    h.message("Page.frameRequestedNavigation", page.session, frameId=page.target, url="notion://x")
    h.message("Page.frameRequestedNavigation", page.session, frameId="child", url="notion://x")
    await h.settle()
    assert page.target == "t2" and page.session == "s-t2" and page.url == "https://notion.so"
    assert old not in h.profile.sessions and h.profile.sessions["s-t2"] == "c1"
    assert ("Target.closeTarget", {"targetId": "t1"}, None) in h.fake.calls


async def test_a_failing_event_task_is_quiet(tmp_path):
    h = await Harness(tmp_path, **{"Runtime.evaluate": CdpError("detached")}).start()
    page = await opened(h)
    h.message("Page.loadEventFired", page.session)
    await h.settle()
    assert h.events == []


async def test_a_renewed_page_whose_old_target_is_gone(tmp_path):
    h = await Harness(tmp_path, **{"Target.closeTarget": CdpError("gone")}).start()
    page = await opened(h, keep=True)
    await h.profile._renew(page, "https://notion.so")
    assert page.target == "t2"


async def test_text_of_a_page(h):
    page = await opened(h)
    page.title = "T"
    assert await h.profile.text("c1", limit=10) == {
        "cell": "c1",
        "url": "https://p.example.com/",
        "title": "T",
        "text": "Title",
    }
    assert "slice(0, 10)" in h.fake.calls[-1][1]["expression"]
    assert await h.profile.text("none") is None


async def test_stop_cancels_tasks_and_stops_chromium(h):
    await opened(h)
    blocker = asyncio.get_running_loop().create_future()
    h.profile._later(blocker)
    tasks = list(h.profile.tasks)
    await h.profile.stop()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert h.fake.stopped and h.profile.pages == {} and all(t.cancelled() for t in tasks)


async def test_stop_before_start(tmp_path):
    await Harness(tmp_path).profile.stop()
