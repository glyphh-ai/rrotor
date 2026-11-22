"""The API's application: the request's context (who it is for, forgotten after it; its span, with a drain), the
platform's few reads from a browser (rrotord/api/cors.py), the errors, a cluster's refusal and its seqs, the gate,
the routes; and, in a cluster, what a forwarded request does here (rrotord/api/forward.py)."""

from __future__ import annotations

from aiohttp import web
from aiohttp.typedefs import Middleware

from rrotord.access import Access
from rrotord.api import forward
from rrotord.api.cors import cors
from rrotord.api.errors import AUDIT, Handler, errors
from rrotord.api.gate import gate
from rrotord.api.routes import BODY_MAX, Services, setup
from rrotord.audit import WHO
from rrotord.cluster.node import Cluster
from rrotord.errors import Unavailable
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


def refused(cluster: Cluster | None) -> Middleware:
    """A node the cluster refused (past max_nodes, removed), or one an unlicensed cluster does not serve from, serves
    nothing but `/healthz`, `/readyz`, the health and `GET /v1/cluster` (which say why): 503 with the refusal's code,
    its /data kept, until it is let in."""

    @web.middleware
    async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        refusal = cluster.refusal if cluster is not None and cluster.status in ("refused", "unlicensed") else None
        if refusal is None or request.path in ("/healthz", "/readyz", "/v1/health", "/v1/cluster"):
            return await handler(request)
        raise Unavailable(str(refusal["message"]), code=str(refusal["code"]))

    return middleware


def seqs(cluster: Cluster | None) -> Middleware:
    """A member's answers carry `Rrotor-Cluster-Seq` (the head's last seq, a follower's applied one); a request's
    `Rrotor-Min-Seq` is the seq a read must see (forward.py)."""

    @web.middleware
    async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        sent = request.headers.get(forward.MIN_SEQ_HEADER, "")
        token = forward.MIN_SEQ.set(int(sent) if sent.isdigit() else 0)
        try:
            response = await handler(request)
        finally:
            forward.MIN_SEQ.reset(token)
        if cluster is not None and cluster.member:
            response.headers[forward.SEQ_HEADER] = str(cluster.replication.applied_seq)
        return response

    return middleware


def make(services: Services) -> web.Application:
    access = services.access or Access(services.config.data)
    telemetry = services.drain.telemetry if services.drain is not None else None
    app = web.Application(
        middlewares=[
            context(telemetry),
            cors(services.config.platform_origin),
            errors,
            refused(services.cluster),
            seqs(services.cluster),
            gate(
                services.config,
                access,
                services.introspect,
                services.browser,
                lambda: services.credentials.runtime_id or services.config.runtime_id,
            ),
        ],
        client_max_size=BODY_MAX,
    )
    if services.audit is not None:
        app[AUDIT] = services.audit
    setup(app, services)
    if services.cluster is not None:
        services.cluster.serve_forward = lambda request: forward.serve(services, request)
    return app
