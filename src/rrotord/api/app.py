"""The API's application: the gate, the errors, the routes."""

from __future__ import annotations

from aiohttp import web

from rrotord.access import Access
from rrotord.api.errors import errors
from rrotord.api.gate import gate
from rrotord.api.routes import BODY_MAX, Services, setup


def make(services: Services) -> web.Application:
    access = services.access or Access(services.config.data)
    app = web.Application(
        middlewares=[errors, gate(services.config, access, services.introspect)], client_max_size=BODY_MAX
    )
    setup(app, services)
    return app
