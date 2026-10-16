"""One Chromium per named profile: one page per cell, each screencasting JPEG frames, input replayed over CDP, popups
and native links by the browser's rules (rules.py)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import platform
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rrotord.browser import rules
from rrotord.browser.cdp import Cdp, CdpError

WORLD = "glyphh"
Frame = Callable[[str, bytes, float], None]
Emit = Callable[[dict[str, Any]], None]
Factory = Callable[[Callable[[dict[str, Any]], None]], Cdp]


def popup_cell(cell: str) -> str:
    return f"{cell}#popup"


@dataclass(slots=True)
class Page:
    cell: str
    target: str
    session: str
    url: str = ""
    title: str = ""
    w: int = 0
    h: int = 0
    scale: float = 1.0
    live: bool = True
    keep: bool = False
    parent: str | None = None
    opening: str | None = None


@dataclass(slots=True)
class Profile:
    name: str
    folder: Path
    chromium: str
    on_frame: Frame
    emit: Emit
    quality: int = 80
    factory: Factory = Cdp
    pages: dict[str, Page] = field(default_factory=dict)
    sessions: dict[str, str] = field(default_factory=dict)
    cdp: Cdp | None = None
    agent: dict[str, Any] | None = None
    tasks: set[asyncio.Task[None]] = field(default_factory=set)

    def args(self) -> list[str]:
        return [
            "--headless=new",
            "--remote-debugging-pipe",
            f"--user-data-dir={self.folder}",
            "--no-first-run",
            "--no-default-browser-check",
            "--hide-scrollbars",
            "--mute-audio",
            "--disable-dev-shm-usage",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "about:blank",
        ]

    async def start(self, env: dict[str, str]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self.cdp = self.factory(self.on_message)
        await self.cdp.start(self.chromium, self.args(), env)
        await self.send("Target.setDiscoverTargets", {"discover": True})

    @property
    def running(self) -> bool:
        return self.cdp is not None and not self.cdp.closed.is_set()

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session: str | None = None,
    ) -> dict[str, Any]:
        assert self.cdp is not None  # noqa: S101  (started first)
        return await self.cdp.send(method, params, session)

    def _later(self, work: Any) -> None:
        async def quiet() -> None:
            with contextlib.suppress(CdpError, KeyError, ValueError):
                await work

        task = asyncio.create_task(quiet())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # events

    def on_message(self, msg: dict[str, Any]) -> None:
        if "sessionId" not in msg:
            self._browser_event(msg)
            return
        page = self.pages.get(self.sessions.get(msg["sessionId"], ""))
        if page is None:
            return
        method, params = msg.get("method"), msg.get("params") or {}
        if method == "Page.windowOpen":
            page.opening = params.get("url")
        elif method == "Runtime.bindingCalled" and params.get("name") == rules.NATIVE_BINDING:
            self._later(self._native(page, str(params.get("payload", "")), False))
        elif method == "Page.frameRequestedNavigation" and params.get("frameId") == page.target:
            self._later(self._native(page, str(params.get("url", "")), True))
        elif method == "Page.screencastFrame":
            self._later(self.send("Page.screencastFrameAck", {"sessionId": params.get("sessionId")}, page.session))
            if page.live:
                captured = float((params.get("metadata") or {}).get("timestamp") or 0) * 1000
                self.on_frame(page.cell, base64.b64decode(params.get("data", "")), captured)
        elif method == "Page.frameNavigated" and not (params.get("frame") or {}).get("parentId"):
            page.url = params["frame"].get("url", "")
            self.emit({"t": "nav", "cell": page.cell, "url": page.url})
        elif method == "Page.loadEventFired":
            self._later(self._title(page))

    async def _title(self, page: Page) -> None:
        r = await self.send("Runtime.evaluate", {"expression": "document.title", "returnByValue": True}, page.session)
        page.title = str((r.get("result") or {}).get("value") or "")
        self.emit({"t": "title", "cell": page.cell, "title": page.title})

    def _browser_event(self, msg: dict[str, Any]) -> None:
        params = msg.get("params") or {}
        if msg.get("method") == "Target.targetCreated":
            info = params.get("targetInfo") or {}
            if info.get("type") != "page" or not info.get("openerId"):
                return
            opener = next((p for p in self.pages.values() if p.target == info["openerId"]), None)
            if opener is not None:
                self._later(self._popup(opener, info))
        elif msg.get("method") == "Target.targetDestroyed":
            gone = next((p for p in self.pages.values() if p.target == params.get("targetId") and p.parent), None)
            if gone is not None:
                self._forget(gone)

    async def _popup(self, opener: Page, info: dict[str, Any]) -> None:
        url = info.get("url") or opener.opening or ""
        opener.opening = None
        decision = rules.popup_decision(url, opener.keep, opener.url)
        if decision["action"] != "own":
            with contextlib.suppress(CdpError):
                await self.send("Target.closeTarget", {"targetId": info["targetId"]})
            if decision["action"] == "same":
                await self.send("Page.navigate", {"url": decision["url"]}, opener.session)
            return
        cell = popup_cell(opener.cell)
        was = self.pages.get(cell)
        if was is not None:
            with contextlib.suppress(CdpError):
                await self.send("Target.closeTarget", {"targetId": was.target})
        attached = await self.send("Target.attachToTarget", {"targetId": info["targetId"], "flatten": True})
        page = Page(cell, info["targetId"], attached["sessionId"], url=url, parent=opener.cell)
        self.pages[cell], self.sessions[page.session] = page, cell
        await self._prepare(page)
        await self._size(page, opener.w, opener.h, opener.scale)
        self.emit({"t": "popup", "cell": opener.cell, "popup": cell, "url": url})

    async def _native(self, page: Page, url: str, reached: bool) -> None:
        decision = rules.native_decision(url, page.keep, page.url)
        if decision is None or decision["action"] == "ignore":
            return
        if reached:
            await self._renew(page, decision["url"])
        else:
            await self.send("Page.navigate", {"url": decision["url"]}, page.session)

    async def _renew(self, page: Page, url: str) -> None:
        old_target, old_session = page.target, page.session
        made = await self.send("Target.createTarget", {"url": "about:blank", "newWindow": True, "background": False})
        attached = await self.send("Target.attachToTarget", {"targetId": made["targetId"], "flatten": True})
        self.sessions.pop(old_session, None)
        page.target, page.session, page.url = made["targetId"], attached["sessionId"], url
        self.sessions[page.session] = page.cell
        await self._prepare(page)
        await self._size(page, page.w, page.h, page.scale)
        await self.send("Page.navigate", {"url": url}, page.session)
        with contextlib.suppress(CdpError):
            await self.send("Target.closeTarget", {"targetId": old_target})

    def _forget(self, page: Page) -> None:
        self.pages.pop(page.cell, None)
        self.sessions.pop(page.session, None)
        self.emit({"t": "popup-closed", "cell": page.parent, "popup": page.cell})

    # pages

    async def _user_agent(self) -> dict[str, Any]:
        if self.agent is None:
            version = await self.send("Browser.getVersion")
            ua = rules.clean_user_agent(str(version.get("userAgent", "")))
            self.agent = {
                "userAgent": ua,
                "userAgentMetadata": rules.user_agent_metadata(ua, platform.machine() in ("arm64", "aarch64")),
            }
        return self.agent

    async def _prepare(self, page: Page) -> None:
        s = page.session
        await self.send("Page.enable", {}, s)
        await self.send("Emulation.setFocusEmulationEnabled", {"enabled": True}, s)
        with contextlib.suppress(CdpError):
            await self.send("Page.setWebLifecycleState", {"state": "active"}, s)
        ua = await self._user_agent()
        await self.send("Network.setUserAgentOverride", ua, s)
        await self.send("Emulation.setUserAgentOverride", ua, s)
        await self.send("Runtime.enable", {}, s)
        await self.send("Runtime.addBinding", {"name": rules.NATIVE_BINDING, "executionContextName": WORLD}, s)
        await self.send(
            "Page.addScriptToEvaluateOnNewDocument", {"source": rules.NATIVE_LINK_SCRIPT, "worldName": WORLD}, s
        )
        try:
            world = await self.send("Page.createIsolatedWorld", {"frameId": page.target, "worldName": WORLD}, s)
            await self.send(
                "Runtime.evaluate",
                {"expression": rules.NATIVE_LINK_SCRIPT, "contextId": world["executionContextId"]},
                s,
            )
        except (CdpError, KeyError):
            pass

    async def _cast(self, page: Page) -> None:
        with contextlib.suppress(CdpError):
            await self.send("Page.stopScreencast", {}, page.session)
        if page.live:
            await self.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": self.quality,
                    "maxWidth": page.w,
                    "maxHeight": page.h,
                    "everyNthFrame": 1,
                },
                page.session,
            )

    async def _size(self, page: Page, w: float, h: float, scale: float = 1.0) -> None:
        page.w, page.h, page.scale = max(64, round(w)), max(48, round(h)), scale
        await self.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": round(page.w / scale),
                "height": round(page.h / scale),
                "deviceScaleFactor": scale,
                "mobile": False,
            },
            page.session,
        )
        await self._cast(page)

    async def open(self, cmd: dict[str, Any]) -> Page:
        cell, url = str(cmd["cell"]), str(cmd.get("url") or "about:blank")
        w, h, scale = float(cmd.get("w") or 800), float(cmd.get("h") or 600), float(cmd.get("scale") or 1)
        page = self.pages.get(cell)
        if page is not None:
            page.keep = bool(cmd.get("keepInPanel"))
            if page.w != round(w) or page.h != round(h) or page.scale != scale:
                page.live = True
                await self._size(page, w, h, scale)
            elif not page.live:
                page.live = True
                await self._cast(page)
            return page
        made = await self.send("Target.createTarget", {"url": "about:blank", "newWindow": True, "background": False})
        attached = await self.send("Target.attachToTarget", {"targetId": made["targetId"], "flatten": True})
        page = Page(cell, made["targetId"], attached["sessionId"], url=url, keep=bool(cmd.get("keepInPanel")))
        self.pages[cell], self.sessions[page.session] = page, cell
        await self._prepare(page)
        await self._size(page, w, h, scale)
        await self.send("Page.navigate", {"url": url}, page.session)
        return page

    async def op(self, cmd: dict[str, Any]) -> None:
        if cmd.get("op") == "open":
            await self.open(cmd)
            return
        page = self.pages.get(str(cmd.get("cell")))
        if page is None:
            return
        s, kind = page.session, cmd.get("op")
        if kind == "resize":
            await self._size(
                page,
                float(cmd.get("w") or page.w),
                float(cmd.get("h") or page.h),
                float(cmd.get("scale") or page.scale),
            )
        elif kind == "hide":
            page.live = False
            await self.send("Page.stopScreencast", {}, s)
        elif kind == "navigate":
            await self.send("Page.navigate", {"url": rules.address_of(str(cmd.get("url", "")))}, s)
        elif kind in ("back", "forward"):
            await self.send("Runtime.evaluate", {"expression": f"history.{kind}()"}, s)
        elif kind == "reload":
            await self.send("Page.reload", {}, s)
        elif kind == "mouse":
            await self.send(*rules.mouse_call(cmd), s)
        elif kind == "wheel":
            await self.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": cmd.get("x", page.w / 2),
                    "y": cmd.get("y", page.h / 2),
                    "deltaX": cmd.get("dx", 0),
                    "deltaY": cmd.get("dy", 0),
                },
                s,
            )
        elif kind == "key":
            for method, params in rules.key_calls(cmd):
                await self.send(method, params, s)
        elif kind == "text":
            await self.send("Input.insertText", {"text": str(cmd.get("text", ""))}, s)
        elif kind == "close":
            if page.parent:
                self._forget(page)
            else:
                self.pages.pop(page.cell, None)
                self.sessions.pop(s, None)
            await self.send("Target.closeTarget", {"targetId": page.target})

    async def text(self, cell: str, limit: int = 60_000) -> dict[str, Any] | None:
        page = self.pages.get(cell)
        if page is None:
            return None
        r = await self.send(
            "Runtime.evaluate",
            {
                "expression": f'(document.body ? document.body.innerText : "").slice(0, {int(limit)})',
                "returnByValue": True,
            },
            page.session,
        )
        return {
            "cell": cell,
            "url": page.url,
            "title": page.title,
            "text": str((r.get("result") or {}).get("value") or ""),
        }

    async def stop(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        if self.cdp is not None:
            await self.cdp.stop()
        self.pages.clear()
        self.sessions.clear()
