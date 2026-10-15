"""Refusals as {"error": {"code", "message", ...}}; anything unexpected is E_INTERNAL, logged, its detail kept out."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiohttp import web

from rrotord.errors import RrotorError

log = logging.getLogger("rrotor.api")
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@web.middleware
async def errors(request: web.Request, handler: Handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except RrotorError as err:
        return web.json_response(err.to_json(), status=err.status)
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
