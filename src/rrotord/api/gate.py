"""Who may call the local API: the mod that holds the local token, from this machine. Host must be this API's
loopback address (no DNS rebinding), an Origin other than its own is refused (no web page may call it), and the bearer
token is compared by its SHA-256, in constant time."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Awaitable, Callable

from aiohttp import web
from aiohttp.typedefs import Middleware

from rrotord.errors import Forbidden, Unauthorized

OPEN = frozenset({"/healthz"})

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def same(token: str, digest: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), digest)


def presented(request: web.Request) -> str:
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return ""


def gate(digest: str | None, ports: tuple[int, ...]) -> Middleware:
    hosts = {f"{name}:{port}" for name in ("127.0.0.1", "localhost") for port in ports}
    origins = {f"http://{h}" for h in hosts}

    @web.middleware
    async def check(request: web.Request, handler: Handler) -> web.StreamResponse:
        if request.path in OPEN:
            return await handler(request)
        if request.host not in hosts:
            raise Forbidden("This API answers only on its loopback address.", code="E_HOST")
        origin = request.headers.get("Origin")
        if origin is not None and origin not in origins:
            raise Forbidden("No web page may call this API.", code="E_ORIGIN")
        token = presented(request)
        if digest is None or not token or not same(token, digest):
            raise Unauthorized("Send the local token: Authorization: Bearer <local token>.")
        return await handler(request)

    return check
