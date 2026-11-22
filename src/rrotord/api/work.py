"""The work routes (docs/architecture.md, 7.1): what the platform's work panels read of this machine, from the
person's own browser (rrotord/api/cors.py), with a bearer every caller sends or a work ticket (rrotord/ticket.py).
The reading itself is rrotord/work.py; it blocks on the disk and on git, so each runs in a thread.

    GET /v1/work                          the workspaces: the crew's clones
    GET /v1/work/{ws}/tree?under=         one folder's entries
    GET /v1/work/{ws}/file?path=&ref=     a file's text; with raw=1, its bytes
    PUT /v1/work/{ws}/file?path=          {text}: the file saved in the working tree
    GET /v1/work/{ws}/diff?branch=&base=  what a branch, or the working tree, changed since its base
    GET /v1/work/{ws}/terminal?after=     the command log of the turns taken there, read only

In a cluster the clones are the head's (a program's data folder is the head's alone), and a file's bytes do not
travel node to node: a follower answers 409 `E_HEAD_ONLY`, and the caller asks the head."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from aiohttp import web

from rrotord.api import forward
from rrotord.errors import BadRequest, Conflict
from rrotord.work import PROGRAM, Work

if TYPE_CHECKING:
    from rrotord.api.routes import Services

PRIVATE = {"Cache-Control": "no-store"}
RAW = PRIVATE | {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"}


async def text_of(request: web.Request) -> str:
    """A save's body: {"text": "..."}."""
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        raise BadRequest('Send {"text": the file\'s whole text}.')
    return str(data["text"])


def setup(app: web.Application, s: Services) -> None:
    work = Work(s.programs.data(PROGRAM))

    def here() -> None:
        if forward.follower(s):
            raise Conflict("The work is on this cluster's head: ask it there.", code="E_HEAD_ONLY")

    def answer(data: dict[str, object]) -> web.Response:
        return web.json_response(data, headers=PRIVATE)

    async def workspaces(_: web.Request) -> web.Response:
        here()
        return answer(await asyncio.to_thread(work.workspaces))

    async def tree(request: web.Request) -> web.Response:
        here()
        return answer(await asyncio.to_thread(work.tree, request.match_info["ws"], request.query.get("under", "")))

    async def file_get(request: web.Request) -> web.Response:
        here()
        ws, q = request.match_info["ws"], request.query
        path, ref = q.get("path", ""), q.get("ref") or None
        if q.get("raw") == "1":
            data, kind = await asyncio.to_thread(work.raw, ws, path, ref)
            return web.Response(body=data, content_type=kind, headers=RAW)
        return answer(await asyncio.to_thread(work.file, ws, path, ref))

    async def file_put(request: web.Request) -> web.Response:
        here()
        ws, text = request.match_info["ws"], await text_of(request)
        saved = await asyncio.to_thread(work.write, ws, request.query.get("path", ""), text)
        if s.audit is not None:
            done = {"status": "done", "code": None}
            s.audit.emit("work.write", workspace=ws, path=saved["path"], size=saved["size"], outcome=done)
        return answer(saved)

    async def diff(request: web.Request) -> web.Response:
        here()
        q = request.query
        got = await asyncio.to_thread(
            work.diff, request.match_info["ws"], q.get("branch") or None, q.get("base") or None
        )
        return answer(got)

    async def terminal(request: web.Request) -> web.Response:
        here()
        after = request.query.get("after", "0")
        if not after.isdigit():
            raise BadRequest("after is the line number to read from.")
        return answer(await asyncio.to_thread(work.terminal, request.match_info["ws"], int(after)))

    app.router.add_get("/v1/work", workspaces)
    app.router.add_get("/v1/work/{ws}/tree", tree)
    app.router.add_get("/v1/work/{ws}/file", file_get)
    app.router.add_put("/v1/work/{ws}/file", file_put)
    app.router.add_get("/v1/work/{ws}/diff", diff)
    app.router.add_get("/v1/work/{ws}/terminal", terminal)
