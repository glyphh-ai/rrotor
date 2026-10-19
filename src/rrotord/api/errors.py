"""Refusals as {"error": {"code", "message", ...}}; anything unexpected is E_INTERNAL, logged, its detail kept out.
Every 401 and 403 is an audit event, `access.denied`, with its code: the caller as far as the gate knew it, else
`anonymous` with the SHA-256 of the bearer sent, if any (never the bearer)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiohttp import web

from rrotord.audit import WHO, Audit, Who, client_of, digest
from rrotord.errors import RrotorError

log = logging.getLogger("rrotor.api")
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
AUDIT = web.AppKey("rrotor_audit", Audit)


def denied(request: web.Request, err: RrotorError) -> None:
    audit = request.app.get(AUDIT)
    if audit is None:
        return
    who = WHO.get()
    if who is None:
        scheme, _, value = request.headers.get("Authorization", "").partition(" ")
        sent = value.strip() if scheme.lower() == "bearer" else ""
        actor = {"type": "anonymous", "key_sha256": digest(sent) if sent else None}
        who = Who(actor, client_of(request), "mcp" if request.path == "/mcp" else "rest")
    outcome = {"status": err.status, "code": err.code}
    audit.emit("access.denied", who=who, method=request.method, path=request.path, outcome=outcome)


@web.middleware
async def errors(request: web.Request, handler: Handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except RrotorError as err:
        if err.status in (401, 403):
            denied(request, err)
        return web.json_response(err.to_json(), status=err.status, headers=err.headers or None)
    except web.HTTPException as err:
        if err.status < 400:
            raise
        code = {404: "E_NOT_FOUND", 405: "E_METHOD", 413: "E_TOO_BIG"}.get(err.status, "E_BAD_REQUEST")
        return web.json_response({"error": {"code": code, "message": err.reason}}, status=err.status)
    except Exception:
        log.exception("local API error", extra={"path": request.path})
        return web.json_response(
            {"error": {"code": "E_INTERNAL", "message": "Something went wrong in rrotor."}}, status=500
        )
