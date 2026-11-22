"""Who may call the API.

Locally: the caller that holds the local token, from this machine. Host must be this API's loopback address (no DNS
rebinding), an Origin other than its own is refused (no web page may call it), and the bearer token is compared by
its SHA-256, in constant time.

Hosted: anyone with a runtime key, or signed in to Glyphh for this runtime (a connector's token the Cloud vouches
for, rrotord/introspect.py), at this machine's own address, `<machine>.<domain>`. A request for another machine's
address is answered with `fly-replay: instance=<machine>`, and Fly's proxy carries it there (the doorman machine
answers every request this way); any other Host is 421. A refusal names where to sign in (`WWW-Authenticate`, RFC
9728), and the resource metadata it points at is open. A paused rrotor (the plan's hours spent) answers 402 to
everything but its health. Each request served with a runtime key is counted for that key (access.Usage); a Glyphh
sign-in is no key and is not counted.

Self-hosted: the hosted gate at the one exact host it was given (RROTOR_PUBLIC_HOST), with nothing replayed and
never paused (its owner's machine has no hosted hours).

Whoever the gate lets in is who the audit log names for what the request does (rrotord/audit.py): the local token's
`local`, a runtime key's SHA-256, a sign-in's subject; with the request's address and its route (rest or mcp)."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.typedefs import Middleware

from rrotord import ticket
from rrotord.access import Access
from rrotord.api.cors import reads
from rrotord.audit import WHO, Who, client_of
from rrotord.browser import Browser
from rrotord.config import Config
from rrotord.errors import Forbidden, RrotorError, Unauthorized
from rrotord.introspect import NOBODY, Introspector

OPEN = frozenset({"/healthz", "/readyz"})
METADATA = "/.well-known/oauth-protected-resource"
SIGN_IN = frozenset({METADATA, METADATA + "/mcp"})  # open on a hosted rrotor: where a client finds how to sign in
SCOPE = "account"
MACHINE = re.compile(r"^[0-9a-z]{6,32}$")
REPLAY = "fly-replay"

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def admit(request: web.Request, actor: dict[str, Any]) -> None:
    """Who the request is for, from here on (the app's outer middleware forgets it after the request)."""
    WHO.set(Who(actor, client_of(request), "mcp" if request.path == "/mcp" else "rest"))


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


def public_url(request: web.Request, exact: str | None = None) -> str:
    """This runtime's address as the client sees it: https, the one exact host it was given, else the Host without
    a default port."""
    if exact is not None:
        return f"https://{exact}"
    host = request.host.lower().rstrip(".")
    return "https://" + (host[:-4] if host.endswith(":443") else host)


def challenge(request: web.Request, token_sent: bool, exact: str | None = None) -> Unauthorized:
    """401, naming the resource metadata so the client can find where to sign in (RFC 9728 section 5.1)."""
    value = f'Bearer resource_metadata="{public_url(request, exact)}{METADATA}/mcp", scope="{SCOPE}"'
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


def viewer_roles(actor: dict[str, Any]) -> set[str] | None:
    """What an app's `roles` is matched against for this caller: a sign-in's role and groups; None for the local
    token (the person's own machine: every app opens, roles ignored)."""
    if actor.get("type") == "local":
        return None
    roles = {str(actor.get("role") or "")} | {str(g) for g in actor.get("groups") or []}
    return {r.lower() for r in roles if r}


def browsing(path: str) -> bool:
    """A path a browser reaches with its session cookie: the apps, the sign-in, and the apps' own API."""
    return path.startswith(("/apps/", "/auth/")) or path == "/v1/apps" or path.startswith("/v1/apps/")


def gate(
    config: Config,
    access: Access,
    introspect: Introspector | None = None,
    browser: Browser | None = None,
    runtime_id: Callable[[], str | None] = lambda: None,
) -> Middleware:
    """The local gate (the local token on loopback) or the hosted one (a runtime key, or a sign-in the Cloud vouches
    for, at this machine's address), which a self-hosted rrotor uses too. A browser's session (rrotord/browser.py)
    is taken on the apps' paths only, its access token introspected like a bearer; a page asked for with no
    session is sent to sign in (docs/architecture.md, 5.8). On the work routes, and only there, both gates also take
    a work ticket Glyphh signed for this runtime (`runtime_id` says which it is now; rrotord/ticket.py), and a page
    of the platform may be the one asking (rrotord/api/cors.py)."""
    hosts = {f"{name}:{port}" for name in ("127.0.0.1", "localhost") for port in (config.host_port, config.local_port)}
    origins = {f"http://{h}" for h in hosts}

    def ticketed(request: web.Request, token: str) -> dict[str, Any] | None:
        """The sign-in a work ticket names, on a path a ticket opens; None for anything else."""
        sub = ticket.subject(token, runtime_id()) if token and ticket.covers(request.path) else None
        return None if sub is None else {"type": "sign-in", "sub": sub}

    @web.middleware
    async def local(request: web.Request, handler: Handler) -> web.StreamResponse:
        if request.path in OPEN:
            return await handler(request)
        if request.host not in hosts:
            raise Forbidden("This API answers only on its loopback address.", code="E_HOST")
        origin = request.headers.get("Origin")
        if origin is not None and origin not in origins and not reads(request, config.platform_origin):
            raise Forbidden("No web page may call this API.", code="E_ORIGIN")
        if request.path.startswith("/apps/"):  # the person's own machine: its browser opens every app
            admit(request, {"type": "local"})
            return await handler(request)
        token = presented(request)
        digest = config.local_token_sha256
        person = ticketed(request, token)
        if person is not None:
            admit(request, person)
            return await handler(request)
        if digest is None or not token or not same(token, digest):
            raise Unauthorized("Send the local token: Authorization: Bearer <local token>.")
        admit(request, {"type": "local"})
        return await handler(request)

    async def session_actor(request: web.Request) -> dict[str, Any] | None:
        """The browser's session, introspected: the sign-in's actor, or None (no session, or not live)."""
        if browser is None or introspect is None:
            return None
        session = await browser.session(request)
        if session is None:
            return None
        who = await introspect.who(session.access_token)
        if not who.active:
            browser.drop(session.id)
            return None
        browser.remember(session, who.sub)
        return who.actor()

    def own_origin(request: web.Request) -> None:
        """A browser's request carries its page's Origin on a cross-site or non-GET request: only this host's."""
        origin = request.headers.get("Origin")
        if origin is not None and origin != public_url(request, config.public_host):
            raise Forbidden("Only this runtime's own pages call it.", code="E_ORIGIN")

    @web.middleware
    async def hosted(request: web.Request, handler: Handler) -> web.StreamResponse:
        if request.path in OPEN:
            return await handler(request)
        elsewhere = route_by_host(request, config.public_domain, config.machine_id, config.public_host)
        if elsewhere is not None:
            return elsewhere
        if request.path in SIGN_IN or request.path.startswith("/auth/"):
            return await handler(request)
        token = presented(request)
        if not token and browsing(request.path):
            own_origin(request)
            actor = await session_actor(request)
            if actor is not None:
                admit(request, actor)
                return await handler(request)
            if request.method == "GET" and request.path.startswith("/apps/") and browser is not None:
                raise web.HTTPFound(f"/auth/start?{urlencode({'next': request.path_qs}, safe='/')}")
            raise challenge(request, False, config.public_host)
        if not token:
            raise challenge(request, False, config.public_host)
        key = access.match(token)
        person = ticketed(request, token) if key is None else None
        who = NOBODY if key is not None or person is not None or introspect is None else await introspect.who(token)
        if key is None and person is None and not who.active:
            raise challenge(request, True, config.public_host)
        admit(request, {"type": "key", "key_sha256": key} if key is not None else person or who.actor())
        if access.paused is not None and config.mode == "hosted":
            raise RrotorError(
                "This runtime is paused: its plan's hosted hours for the month are spent.",
                code="E_LIMIT",
                status=402,
                limit="runtime_hours",
                reason=access.paused.get("reason"),
            )
        if key is not None:
            access.usage.count(key)
        return await handler(request)

    return hosted if config.public else local
