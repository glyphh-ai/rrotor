"""rrotord, put together: the sandbox checked, the runner, the link and the local API, until it is told to stop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass, field

import aiohttp
from aiohttp import web

from rrotord import VERSION
from rrotord.api.app import make
from rrotord.api.routes import Services
from rrotord.config import Config
from rrotord.deploy import Deployer
from rrotord.errors import RrotorError
from rrotord.keys import public_key
from rrotord.licence import Licence
from rrotord.link.client import LinkClient
from rrotord.link.credentials import Credentials
from rrotord.link.handler import LinkHandler
from rrotord.link.outbox import Outbox
from rrotord.models import Models
from rrotord.parts import Parts
from rrotord.proxy import EgressProxy
from rrotord.runner.engine import Runner
from rrotord.runner.launch import Launcher, SandboxLauncher
from rrotord.runner.record import Records
from rrotord.sandbox.probe import Layers, probe

log = logging.getLogger("rrotor")
EXPIRE_EVERY = 30.0


@dataclass(slots=True)
class Daemon:
    config: Config
    layers: Layers
    launcher: Launcher = field(default_factory=SandboxLauncher)
    deployer: Deployer = field(init=False)
    parts: Parts = field(init=False)
    licence: Licence = field(init=False)
    runner: Runner = field(init=False)
    link: LinkClient = field(init=False)
    session: aiohttp.ClientSession | None = None

    def __post_init__(self) -> None:
        c = self.config
        models = Models(c.data)
        self.deployer = Deployer(models, c.run_dir / "deploy")
        self.parts = Parts(c.data, public_key(c.release_key) if c.release_key else None)
        self.licence = Licence(c.data, public_key(c.licence_key) if c.licence_key else None)
        handler = LinkHandler(models, None, self.deployer)  # type: ignore[arg-type]
        self.runner = Runner(
            models=models,
            records=Records(c.data),
            proxy=EgressProxy(c.run_dir / "proxy"),
            launcher=self.launcher,
            sdk=c.sdk,
            layers=self.layers,
            notify=handler.notify,
            parts=self.parts,
            licence=self.licence,
        )
        handler.runner = self.runner
        self.link = LinkClient(
            url=c.link_url,
            version=VERSION,
            mode=c.mode,
            credentials=Credentials(c.data),
            outbox=Outbox(c.data / "link"),
            handler=handler,
            connect=self._connect,
        )
        handler.link = self.link

    async def _fetch(self, url: str) -> bytes:
        """A part's tarball by URL (its release asset), at most 256 MB."""
        if self.session is None:
            self.session = aiohttp.ClientSession()
        async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=600)) as res:
            if res.status != 200:
                raise RrotorError(
                    f"The part could not be fetched: {res.status} from {url}.", code="E_PART_FETCH", status=502
                )
            return await res.content.read(256 * 1024 * 1024 + 1)

    async def _connect(self, url: str, headers: dict[str, str]) -> aiohttp.ClientWebSocketResponse:
        if self.session is None:
            self.session = aiohttp.ClientSession()
        return await self.session.ws_connect(
            url, headers=headers, autoping=True, timeout=aiohttp.ClientWSTimeout(ws_close=5)
        )

    def services(self) -> Services:
        return Services(
            self.config,
            self.runner.models,
            self.deployer,
            self.runner,
            self.link.credentials,
            self.link,
            self.layers,
            self.parts,
            self.licence,
            self._fetch,
        )

    async def _expire(self) -> None:
        while True:
            await asyncio.sleep(EXPIRE_EVERY)
            try:
                await self.runner.expire()
            except Exception:
                log.exception("expiring waiting runs failed")

    async def serve(self, stop: asyncio.Event) -> None:
        await self.runner.recover()
        api = web.AppRunner(make(self.services()), access_log=None)
        await api.setup()
        await web.TCPSite(api, "0.0.0.0", self.config.local_port).start()  # noqa: S104  (published to loopback only)
        log.info("rrotor up", extra={"version": VERSION, "mode": self.config.mode, "sandbox": self.layers.ready})
        tasks = [asyncio.create_task(self.link.run()), asyncio.create_task(self._expire())]
        await stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.runner.shutdown()
        await api.cleanup()
        if self.session is not None:
            await self.session.close()
        log.info("rrotor stopped")


async def main(config: Config) -> None:  # pragma: no cover  (the process itself: exercised by the image's tests)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    layers = await probe()
    if not layers.ready:
        log.warning("sandbox not ready: runs are refused", extra={"why": layers.why()})
    daemon = Daemon(config, layers)
    with contextlib.suppress(asyncio.CancelledError):
        await daemon.serve(stop)
