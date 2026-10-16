"""The browser part on the local API: its routes (/frames, /events, /api/ops, /api/text, /api/favicon, and the pages
as /api/browser), the stream and its frame protocol (docs/architecture.md, section 12). Pages are keyed by cell; each
cell's page lives in a named profile's Chromium, shared with programs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from aiohttp import web

from rrotord.browser import rules
from rrotord.browser.cdp import CdpError
from rrotord.browser.chrome import Profile
from rrotord.config import Config
from rrotord.errors import BadRequest, Unavailable

BOUNDARY = "glyphhframe"
PROFILE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
OP_WAIT = 2.0
IDLE = 3600.0
CHROMIUM = ("/usr/bin/chromium", "/usr/lib/chromium/chromium")
_WRITES: set[asyncio.Future[None]] = set()
SECURITY = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}

Fetch = Callable[[str, int], Awaitable[tuple[str, bytes] | None]]


async def fetch_limited(url: str, limit: int) -> tuple[str, bytes] | None:
    """Fetched with a time and a size limit; None on any failure."""
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux) Chrome/140.0.0.0 Safari/537.36"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session, session.get(url) as res:
            if res.status != 200:
                return None
            body = await res.content.read(limit + 1)
            return (res.headers.get("Content-Type", ""), body) if len(body) <= limit else None
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None


class Browser:
    def __init__(
        self,
        chromium: str,
        profiles_root: Path,
        home: Path,
        fetch: Fetch = fetch_limited,
        profile_factory: Callable[..., Profile] = Profile,
    ) -> None:
        self.chromium, self.root, self.home = chromium, profiles_root, home
        self.fetch, self.profile_factory = fetch, profile_factory
        self.profiles: dict[str, Profile] = {}
        self.cells: dict[str, str] = {}  # cell → profile
        self.used: dict[str, float] = {}
        self.last: dict[str, tuple[bytes, float]] = {}
        self.streams: set[web.StreamResponse] = set()
        self.events: set[web.StreamResponse] = set()
        self.ops_lock = asyncio.Lock()
        self.favicons: dict[str, tuple[str, bytes] | None] = {}
        self.error: str | None = None

    @classmethod
    def find(cls, config: Config, env: dict[str, str] | None = None) -> Browser | None:
        env = dict(os.environ) if env is None else env
        found = env.get("RROTOR_CHROMIUM") or next((c for c in CHROMIUM if Path(c).is_file()), None)
        if not found:
            return None
        return cls(found, config.data / "profiles", config.run_dir / "chromium-home")

    def view(self) -> dict[str, Any]:
        return {
            "ready": self.error is None,
            "detail": self.error,
            "chromium": self.chromium,
            "profiles": sorted(n for n, p in self.profiles.items() if p.running),
            "pages": len(self.cells),
        }

    # the stream

    def _frame(self, cell: str, data: bytes, captured: float) -> None:
        self.last[cell] = (data, captured)
        for res in list(self.streams):
            self._frame_to(res, cell, data, captured)

    def _emit(self, message: dict[str, Any]) -> None:
        if message.get("t") == "popup-closed":
            self.last.pop(str(message.get("popup")), None)
            self.cells.pop(str(message.get("popup")), None)
        if message.get("t") == "popup":
            self.cells[str(message["popup"])] = self.cells.get(str(message["cell"]), "default")
        line = f"data: {json.dumps(message)}\n\n".encode()
        for res in list(self.events):
            self._write(res, line, self.events)

    @staticmethod
    def _write(res: web.StreamResponse, data: bytes, group: set[web.StreamResponse]) -> None:
        async def send() -> None:
            try:
                await res.write(data)
            except (ConnectionError, RuntimeError):
                group.discard(res)

        task = asyncio.ensure_future(send())
        _WRITES.add(task)
        task.add_done_callback(_WRITES.discard)

    # profiles

    async def profile(self, name: str) -> Profile:
        if not PROFILE.match(name):
            raise BadRequest("A profile is a lowercase name.", code="E_PROFILE")
        found = self.profiles.get(name)
        if found is not None and found.running:
            return found
        made = self.profile_factory(
            name=name, folder=self.root / name, chromium=self.chromium, on_frame=self._frame, emit=self._emit
        )
        self.home.mkdir(parents=True, exist_ok=True)
        try:
            await made.start({"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home)})
        except (OSError, CdpError) as err:
            self.error = f"Chromium did not start: {err}"
            raise Unavailable(self.error, code="E_BROWSER") from None
        self.error = None
        self.profiles[name] = made
        return made

    async def op(self, cmd: dict[str, Any]) -> None:
        cell = str(cmd.get("cell", ""))
        if cmd.get("op") == "open":
            self.cells[cell] = str(cmd.get("profile") or self.cells.get(cell) or "default")
        name = self.cells.get(cell)
        if name is None:
            return
        self.used[name] = time.monotonic()
        profile = await self.profile(name)
        await profile.op(cmd)
        if cmd.get("op") == "close":
            self.cells.pop(cell, None)
            self.last.pop(cell, None)

    async def reap(self, now: float | None = None) -> list[str]:
        """Profiles with no page, idle for an hour, stop."""
        now = time.monotonic() if now is None else now
        gone = []
        for name, profile in list(self.profiles.items()):
            if name not in self.cells.values() and now - self.used.get(name, 0) >= IDLE:
                await profile.stop()
                del self.profiles[name]
                gone.append(name)
        return gone

    async def run(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self.reap()

    async def shutdown(self) -> None:
        for res in list(self.streams | self.events):
            with contextlib.suppress(Exception):
                await res.write_eof()
        for profile in self.profiles.values():
            await profile.stop()
        self.profiles.clear()

    # routes

    def routes(self, app: web.Application) -> None:
        app.router.add_get("/frames", self.frames)
        app.router.add_get("/events", self.sse)
        app.router.add_post("/api/ops", self.ops)
        app.router.add_get("/api/text", self.text)
        app.router.add_get("/api/browser", self.pages)
        app.router.add_get("/api/favicon", self.favicon)

    async def frames(self, request: web.Request) -> web.StreamResponse:
        res = web.StreamResponse(headers={**SECURITY, "Content-Type": f"multipart/mixed; boundary={BOUNDARY}"})
        await res.prepare(request)
        self.streams.add(res)
        for cell, (data, captured) in list(self.last.items()):
            self._frame_to(res, cell, data, captured)
        await self._hold(request, res, self.streams)
        return res

    def _frame_to(self, res: web.StreamResponse, cell: str, data: bytes, captured: float) -> None:
        part = (
            (
                f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nX-Cell: {_quote(cell)}\r\nX-Captured: {captured:.0f}\r\n"
                f"Content-Length: {len(data)}\r\n\r\n"
            ).encode()
            + data
            + b"\r\n"
        )
        self._write(res, part, self.streams)

    async def sse(self, request: web.Request) -> web.StreamResponse:
        res = web.StreamResponse(headers={**SECURITY, "Content-Type": "text/event-stream"})
        await res.prepare(request)
        await res.write(b": hello\n\n")
        self.events.add(res)
        await self._hold(request, res, self.events)
        return res

    @staticmethod
    async def _hold(request: web.Request, res: web.StreamResponse, group: set[web.StreamResponse]) -> None:
        """Until the client goes: the response stays open, written to by the stream."""
        try:
            while not (request.transport is None or request.transport.is_closing()):  # noqa: ASYNC110
                await asyncio.sleep(1)
        finally:
            group.discard(res)

    async def ops(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BadRequest("The body must be JSON.") from None
        cmds = data if isinstance(data, list) else [data]
        async with self.ops_lock:  # one op after another, across requests too
            for cmd in cmds:
                if isinstance(cmd, dict):
                    with contextlib.suppress(TimeoutError, CdpError, ValueError, KeyError, TypeError):
                        await asyncio.wait_for(self.op(cmd), OP_WAIT if cmd.get("op") != "open" else 30)
        return web.json_response({}, headers=SECURITY)

    async def text(self, request: web.Request) -> web.Response:
        cell = request.query.get("cell", "")
        name = self.cells.get(cell)
        found = await self.profiles[name].text(cell) if name in self.profiles else None
        return web.json_response(found or {"error": "no such page"}, headers=SECURITY)

    async def pages(self, _: web.Request) -> web.Response:
        out = [
            {"cell": p.cell, "url": p.url, "title": p.title, "profile": name}
            for name, prof in self.profiles.items()
            for p in prof.pages.values()
        ]
        return web.json_response({"pages": out}, headers=SECURITY)

    async def favicon(self, request: web.Request) -> web.Response:
        target = rules.favicon_target(request.query.get("url"))
        if not target:
            return web.Response(status=404, headers=SECURITY)
        site = rules.origin(target) or target
        if site not in self.favicons:
            page = await self.fetch(target, 1024 * 1024)
            icon = None
            for url in rules.favicon_candidates(target, page[1].decode(errors="replace") if page else ""):
                got = await self.fetch(url, 512 * 1024)
                if got and rules.is_icon(got[0], len(got[1])):
                    icon = got
                    break
            self.favicons[site] = icon
        icon = self.favicons[site]
        if icon is None:
            return web.Response(status=404, headers=SECURITY)
        return web.Response(
            body=icon[1], headers={**SECURITY, "Content-Type": icon[0], "Cache-Control": "private, max-age=86400"}
        )


def _quote(cell: str) -> str:
    return quote(cell, safe="!*'()")  # as encodeURIComponent
