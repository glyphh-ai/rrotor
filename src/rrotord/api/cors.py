"""The few reads a page of Glyphh's platform makes of this rrotor from the browser itself (what they answer stays
between the machine and the person's browser: Glyphh's server neither carries nor keeps it). Only the paths named
here, only the methods named for each, and only for the platform's one origin (`RROTOR_PLATFORM_ORIGIN`,
https://platform.glyphh.ai): the answer, a refusal too, carries `Access-Control-Allow-Origin` for it, and the
browser's preflight (which carries no credentials, so it is answered before the gate) is told which methods, with
`Authorization`, are fine, on a private network too (a local rrotor on loopback). Nothing is opened by this: the
request itself still passes the gate, with the bearer every caller sends (the local token, a runtime key, a Glyphh
sign-in for this runtime or, on the work routes, a work ticket: rrotord/ticket.py); no cookie is taken across
origins. Any other origin, and any other path, is left as it was: the gate refuses it.

`GET /v1/folders` is the folder picker's; `/v1/work` and what is under it are the work panels' (rrotord/work.py),
which also save a file (`PUT`, a JSON body)."""

from __future__ import annotations

from aiohttp import web
from aiohttp.typedefs import Middleware

from rrotord.api.errors import Handler

PATHS = frozenset({"/v1/folders"})
WORK = "/v1/work"
PREFLIGHT = {
    "Access-Control-Allow-Methods": "GET",
    "Access-Control-Allow-Headers": "Authorization",
    "Access-Control-Allow-Private-Network": "true",
    "Access-Control-Max-Age": "600",
}
WORK_PREFLIGHT = PREFLIGHT | {
    "Access-Control-Allow-Methods": "GET, PUT",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
}


def work(path: str) -> bool:
    """Whether a path is the work panels': `/v1/work` and what is under it."""
    return path == WORK or path.startswith(WORK + "/")


def reads(request: web.Request, origin: str) -> bool:
    """Whether this is one of the platform's reads: its origin, on a path it may read, with a method that path takes
    (or its preflight)."""
    if request.headers.get("Origin") != origin:
        return False
    if work(request.path):
        return request.method in ("GET", "PUT", "OPTIONS")
    return request.path in PATHS and request.method in ("GET", "OPTIONS")


def cors(origin: str) -> Middleware:
    allow = {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}

    @web.middleware
    async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        if not reads(request, origin):
            return await handler(request)
        if request.method == "OPTIONS":
            return web.Response(status=204, headers=allow | (WORK_PREFLIGHT if work(request.path) else PREFLIGHT))
        response = await handler(request)
        response.headers.update(allow)
        return response

    return middleware
