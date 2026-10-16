"""The local API's application: the gate, the errors, the routes."""

from __future__ import annotations

from aiohttp import web

from rrotord.api.errors import errors
from rrotord.api.gate import gate
from rrotord.api.routes import BODY_MAX, Services, setup


def make(services: Services) -> web.Application:
    config = services.config
    app = web.Application(
        middlewares=[errors, gate(config.local_token_sha256, (config.host_port, config.local_port))],
        client_max_size=BODY_MAX,
    )
    setup(app, services)
    return app
