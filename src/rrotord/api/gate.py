"""Who may call the API.

Locally: the mod that holds the local token, from this machine. Host must be this API's loopback address (no DNS
rebinding), an Origin other than its own is refused (no web page may call it), and the bearer token is compared by
its SHA-256, in constant time.

Hosted: anyone with a runtime key, or signed in to Glyphh for this runtime (a connector's token the Cloud vouches
for, rrotord/introspect.py), at this machine's own address, `<machine>.<domain>`. A request for another machine's
address is answered with `fly-replay: instance=<machine>`, and Fly's proxy carries it there (the doorman machine
answers every request this way); any other Host is 421. A refusal names where to sign in (`WWW-Authenticate`, RFC
9728), and the resource metadata it points at is open. A paused rrotor (the plan's hours spent) answers 402 to
everything but its health."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Awaitable, Callable

from aiohttp import web
from aiohttp.typedefs import Middleware

from rrotord.access import Access
from rrotord.config import Config
from rrotord.errors import Forbidden, RrotorError, Unauthorized
from rrotord.introspect import Introspector

OPEN = frozenset({"/healthz"})
METADATA = "/.well-known/oauth-protected-resource"
SIGN_IN = frozenset({METADATA, METADATA + "/mcp"})  # open on a hosted rrotor: where a client finds how to sign in
SCOPE = "account"
MACHINE = re.compile(r"^[0-9a-z]{6,32}$")
REPLAY = "fly-replay"

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def same(token: str, digest: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), digest)


def presented(request: web.Request) -> str:
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return ""


def machine_of(host: str, domain: str) -> str | None:
    """The machine a Host names under the public domain (`<machine>.<domain>`, a port ignored); None for any other."""
    name = host.rsplit(":", 1)[0].lower().rstrip(".")
    label, dot, rest = name.partition(".")
    if not dot or rest != domain.lower() or not MACHINE.match(label):
        return None
    return label


def replay(machine: str) -> web.Response:
    """What Fly's proxy turns into the same request on that machine, started if it was stopped."""
    return web.Response(status=200, headers={REPLAY: f"instance={machine}"})


def public_url(request: web.Request) -> str:
    """This runtime's address as the client sees it: https, the Host without a default port."""
    host = request.host.lower().rstrip(".")
    return "https://" + (host[:-4] if host.endswith(":443") else host)


def challenge(request: web.Request, token_sent: bool) -> Unauthorized:
    """401, naming the resource metadata so the client can find where to sign in (RFC 9728 section 5.1)."""
    value = f'Bearer resource_metadata="{public_url(request)}{METADATA}/mcp", scope="{SCOPE}"'
    if token_sent:
        value += ', error="invalid_token"'
    return Unauthorized(
        "Send a runtime key (Authorization: Bearer <key>), or sign in to Glyphh for this runtime.",
        headers={"WWW-Authenticate": value},
    )


def misdirected(host: str) -> RrotorError:
    return RrotorError(f"This address is not served here: {host}.", code="E_HOST", status=421)


def route_by_host(request: web.Request, domain: str, own: str | None, exact: str | None = None) -> web.Response | None:
    """A replay for another machine's address, nothing for this machine's own (or, in development, for the one exact
    Host it was given); 421 for anything else."""
    if exact is not None:
        if request.host.rsplit(":", 1)[0].lower().rstrip(".") == exact:
            return None
        raise misdirected(request.host)
    machine = machine_of(request.host, domain)
    if machine is None:
        raise misdirected(request.host)
    if own is not None and machine == own:
        return None
    return replay(machine)


def gate(config: Config, access: Access, introspect: Introspector | None = None) -> Middleware:
    """The local gate (the local token on loopback) or the hosted one (a runtime key, or a sign-in the Cloud vouches
    for, at this machine's address)."""
    hosts = {f"{name}:{port}" for name in ("127.0.0.1", "localhost") for port in (config.host_port, config.local_port)}
    origins = {f"http://{h}" for h in hosts}

    @web.middleware
    async def local(request: web.Request, handler: Handler) -> web.StreamResponse:
        if request.path in OPEN:
            return await handler(request)
        if request.host not in hosts:
            raise Forbidden("This API answers only on its loopback address.", code="E_HOST")
        origin = request.headers.get("Origin")
        if origin is not None and origin not in origins:
            raise Forbidden("No web page may call this API.", code="E_ORIGIN")
        token = presented(request)
        digest = config.local_token_sha256
        if digest is None or not token or not same(token, digest):
            raise Unauthorized("Send the local token: Authorization: Bearer <local token>.")
        return await handler(request)

    @web.middleware
    async def hosted(request: web.Request, handler: Handler) -> web.StreamResponse:
        if request.path in OPEN:
            return await handler(request)
        elsewhere = route_by_host(request, config.public_domain, config.machine_id, config.public_host)
        if elsewhere is not None:
            return elsewhere
        if request.path in SIGN_IN:
            return await handler(request)
        token = presented(request)
        if not token:
            raise challenge(request, token_sent=False)
        if not access.allows(token) and not (introspect is not None and await introspect.active(token)):
            raise challenge(request, token_sent=True)
        if access.paused is not None:
            raise RrotorError(
                "This runtime is paused: its plan's hosted hours for the month are spent.",
                code="E_LIMIT",
                status=402,
                limit="runtime_hours",
                reason=access.paused.get("reason"),
            )
        return await handler(request)

    return hosted if config.mode == "hosted" else local
