"""The API's application: the request's context (who it is for, forgotten after it; its span, with a drain), the
errors, the gate, the routes."""

from __future__ import annotations

from aiohttp import web
from aiohttp.typedefs import Middleware

from rrotord.access import Access
from rrotord.api.errors import AUDIT, Handler, errors
from rrotord.api.gate import gate
from rrotord.api.routes import BODY_MAX, Services, setup
from rrotord.audit import WHO
from rrotord.otel import Telemetry


def context(telemetry: Telemetry | None) -> Middleware:
    """Who the request is for is forgotten after it (a kept-alive connection serves the next one in the same task);
    with a drain, the request is a span, a child of the caller's `traceparent`."""

    @web.middleware
    async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        token = WHO.set(None)
        try:
            if telemetry is None:
                return await handler(request)
            resource = request.match_info.route.resource
            route = resource.canonical if resource is not None else request.path
            with telemetry.request_span(request.method, route, request.headers) as span:
                response = await handler(request)
                span.set_attribute("http.response.status_code", response.status)
                return response
        finally:
            WHO.reset(token)

    return middleware


def make(services: Services) -> web.Application:
    access = services.access or Access(services.config.data)
    telemetry = services.drain.telemetry if services.drain is not None else None
    app = web.Application(
        middlewares=[context(telemetry), errors, gate(services.config, access, services.introspect)],
        client_max_size=BODY_MAX,
    )
    if services.audit is not None:
        app[AUDIT] = services.audit
    setup(app, services)
    return app
