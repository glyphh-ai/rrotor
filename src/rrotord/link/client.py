"""The link client: connects out, says hello, keeps the link (pings both ways, answers the Cloud's messages), replays
what is not acknowledged after each reconnect, and reconnects with backoff, 1 s to 30 s with jitter. A close that
says not to retry (4401 refused, 4409 replaced, 4426 upgrade, 4400 bad hello) stops it until a new token is handed
in (docs/architecture.md, section 6)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import aiohttp
from pydantic import ValidationError

from rrotord.link import protocol as p
from rrotord.link.credentials import Credentials, digest
from rrotord.link.outbox import Outbox

log = logging.getLogger("rrotor.link")


class Handler(Protocol):
    def deployed(self) -> list[dict[str, Any]]: ...
    async def on_welcome(self, message: p.Welcome) -> None: ...
    async def on_call(self, message: p.Call) -> None: ...
    async def on_continue(self, message: p.Continue) -> None: ...
    async def on_cancel(self, message: p.Cancel) -> None: ...
    async def on_deploy(self, message: p.Deploy) -> None: ...
    async def on_access(self, message: p.Keys | p.Pause | p.Resume) -> None: ...


Connect = Callable[[str, dict[str, str]], Awaitable[aiohttp.ClientWebSocketResponse]]


class LinkClient:
    PING_EVERY, MISSED, WELCOME_WAIT, BACKOFF_MAX = 20.0, 3, 10.0, 30.0

    def __init__(
        self,
        *,
        url: str,
        version: str,
        mode: str,
        credentials: Credentials,
        outbox: Outbox,
        handler: Handler,
        connect: Connect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.url, self.version, self.mode = url, version, mode
        self.credentials, self.outbox, self.handler = credentials, outbox, handler
        self.connect, self.sleep = connect, sleep
        self.state, self.since = "no_token", time.time()
        self.last_close: dict[str, Any] | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.attempt = 0
        self.reason: str | None = None  # what the Cloud said when it closed the link
        self.tasks: set[asyncio.Task[None]] = set()

    def _set(self, state: str) -> None:
        if state != self.state:
            log.info("link %s", state, extra={"link": state})
            self.state, self.since = state, time.time()

    def view(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "since": self.since,
            "last_close": self.last_close,
            "owed": len(self.outbox.pending),
            "seq": self.outbox.seq,
        }

    @property
    def configured(self) -> bool:
        """Whether this rrotor has a runtime token: without one there is no Cloud to owe anything (and a first token
        drops whatever was kept)."""
        return self.credentials.token is not None

    async def owe(self, make: Callable[[int], dict[str, Any]], numbered: bool = True) -> None:
        """Keep a message until the Cloud acknowledges it, and send it now if the link is up."""
        message = self.outbox.add(make, numbered)
        await self._send(message)

    async def _send(self, message: dict[str, Any]) -> bool:
        ws = self.ws
        if ws is None or ws.closed or self.state != "online":
            return False
        try:
            await ws.send_str(json.dumps(message, separators=(",", ":")))
        except (ConnectionError, RuntimeError, aiohttp.ClientError):
            return False
        return True

    async def run(self) -> None:
        while True:
            token, runtime_id = self.credentials.token, self.credentials.runtime_id
            if token is None or runtime_id is None or self.state in p.STOPS.values():
                if token is None:
                    self._set("no_token")
                await self.credentials.changed.wait()
                self.credentials.changed.clear()
                self._set("connecting")
                continue
            self.outbox.restart(digest(token))
            self.credentials.changed.clear()
            code = await self._session(token, runtime_id)
            if code in p.STOPS:
                self._set(p.STOPS[code])
                continue
            if self.credentials.changed.is_set():
                self.attempt = 0
                continue
            self._set("backoff")
            delay = min(self.BACKOFF_MAX, 2.0**self.attempt) * random.uniform(0.5, 1.0)  # noqa: S311  (jitter)
            self.attempt += 1
            waiter = asyncio.ensure_future(self.credentials.changed.wait())
            sleeper = asyncio.ensure_future(self.sleep(delay))
            await asyncio.wait({waiter, sleeper}, return_when=asyncio.FIRST_COMPLETED)
            for f in (waiter, sleeper):
                f.cancel()

    async def _session(self, token: str, runtime_id: str) -> int | None:
        """One connection, from connect to close; answers the close code (None when it never connected)."""
        self._set("connecting")
        try:
            ws = await self.connect(self.url, {"Authorization": f"Bearer {token}"})
        except (aiohttp.ClientError, OSError, TimeoutError) as err:
            self.last_close = {"code": None, "reason": type(err).__name__}
            return None
        self.ws, self.reason = ws, None
        try:
            await ws.send_str(json.dumps(p.hello(self.version, self.mode, runtime_id, self.handler.deployed())))
            if not await self._welcomed(ws):
                await ws.close()
                return self._closed(ws)
            self._set("online")
            self.attempt = 0
            for message in list(self.outbox.pending):
                await self._send(message)
            await self._serve(ws)
        except (ConnectionError, RuntimeError, aiohttp.ClientError):
            pass
        finally:
            self.ws = None
            with contextlib.suppress(Exception):
                await ws.close()
        return self._closed(ws)

    def _closed(self, ws: aiohttp.ClientWebSocketResponse) -> int | None:
        code = ws.close_code
        self.last_close = {"code": code, "reason": (self.reason or "")[:200] or None}
        return code

    async def _welcomed(self, ws: aiohttp.ClientWebSocketResponse) -> bool:
        try:
            msg = await ws.receive(timeout=self.WELCOME_WAIT)
        except TimeoutError:
            return False
        if msg.type != aiohttp.WSMsgType.TEXT:
            self._heard_close(msg)
            return False
        try:
            welcome = p.DOWN.validate_json(msg.data)
        except ValidationError:
            return False
        if not isinstance(welcome, p.Welcome):
            return False
        await self.handler.on_welcome(welcome)
        return True

    def _heard_close(self, msg: aiohttp.WSMessage) -> None:
        if msg.type == aiohttp.WSMsgType.CLOSE and msg.extra:
            self.reason = str(msg.extra)

    async def _serve(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        heard = time.monotonic()
        pinger = asyncio.create_task(self._ping(ws, lambda: heard))
        watcher = asyncio.create_task(self._watch(ws))
        try:
            while True:
                msg = await ws.receive()
                if msg.type != aiohttp.WSMsgType.TEXT:
                    self._heard_close(msg)
                    if msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        return
                    continue
                heard = time.monotonic()
                await self._dispatch(msg.data)
        finally:
            for task in (pinger, watcher):
                task.cancel()
            await asyncio.gather(pinger, watcher, return_exceptions=True)

    async def _watch(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """A new token handed in: this link closes, and the next one uses it."""
        await self.credentials.changed.wait()
        await ws.close(code=1000, message=b"new token")

    async def _ping(self, ws: aiohttp.ClientWebSocketResponse, heard: Callable[[], float]) -> None:
        while True:
            await self.sleep(self.PING_EVERY)
            if time.monotonic() - heard() > self.PING_EVERY * self.MISSED:
                await ws.close(code=1000, message=b"reconnecting")
                return
            await self._send(p.Ping().model_dump())

    async def _dispatch(self, raw: str) -> None:
        try:
            message = p.DOWN.validate_json(raw)
        except ValidationError as err:
            log.warning(
                "unreadable message skipped", extra={"errors": err.errors(include_input=False, include_url=False)[:3]}
            )
            return
        if isinstance(message, p.Ping):
            await self._send(p.Pong().model_dump())
        elif isinstance(message, p.Ack):
            self.outbox.ack(message.of)
        elif isinstance(message, p.Call):
            self._spawn(self.handler.on_call(message))
        elif isinstance(message, p.Continue):
            self._spawn(self.handler.on_continue(message))
        elif isinstance(message, p.Cancel):
            self._spawn(self.handler.on_cancel(message))
        elif isinstance(message, p.Deploy):
            self._spawn(self.handler.on_deploy(message))
        elif isinstance(message, p.Keys | p.Pause | p.Resume):
            self._spawn(self.handler.on_access(message))
        elif isinstance(message, p.Unbuilt):
            log.info("message not handled yet", extra={"type": message.type, "code": "E_NOT_BUILT"})

    def _spawn(self, work: Awaitable[None]) -> None:
        async def guarded() -> None:
            try:
                await work
            except Exception:
                log.exception("a message from the Cloud failed")

        task = asyncio.create_task(guarded())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
