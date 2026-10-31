"""The doorman: the one always-on machine in the runtimes app. Fly's proxy lands a request for `<machine>.<domain>`
on any started machine; this one answers every such request with `fly-replay: instance=<machine>`, so the right
machine gets it (started if it was stopped) and nobody else's machine is woken to bounce it. Nothing else: no
programs, no link, no data."""

from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web

from rrotord.api.errors import errors
from rrotord.api.gate import OPEN, route_by_host
from rrotord.config import Config

log = logging.getLogger("rrotor.doorman")


def make(config: Config) -> web.Application:
    async def door(request: web.Request) -> web.StreamResponse:
        if request.path in OPEN:
            return web.json_response({"ok": True, "doorman": True})
        return route_by_host(request, config.public_domain, None) or web.Response(status=421)

    app = web.Application(middlewares=[errors])
    app.router.add_route("*", "/{tail:.*}", door)
    return app


async def serve(config: Config, stop: asyncio.Event) -> None:
    runner = web.AppRunner(make(config), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.local_port).start()  # noqa: S104  (behind Fly's proxy)
    log.info("doorman up", extra={"domain": config.public_domain, "port": config.local_port})
    await stop.wait()
    await runner.cleanup()
    log.info("doorman stopped")


async def main(config: Config) -> None:  # pragma: no cover  (the process itself)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await serve(config, stop)
