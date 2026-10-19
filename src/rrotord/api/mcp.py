"""The runtime's own MCP server, `POST /mcp` (Streamable HTTP, stateless, one JSON-RPC message per POST): Claude adds
the runtime as a connector with a runtime key and talks to it directly. MCP 2026-07-28 (`server/discover`, each
request's _meta naming the version) and the earlier initialize revisions are both served. The tools are the API's
operations (ops.py), so REST and MCP do the same thing."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiohttp import web

from rrotord import VERSION
from rrotord.api import ops
from rrotord.errors import RrotorError

if TYPE_CHECKING:
    from rrotord.api.routes import Services

MODERN = "2026-07-28"
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26")
VERSION_META = "io.modelcontextprotocol/protocolVersion"
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
INSTRUCTIONS = (
    "This person's Glyphh runtime (rrotor): their Ada models, deployed by commit and run here. models_list shows them; "
    "model_call runs a function and answers its Result (done, needs_input, needs_approval or failed, with data); "
    "model_continue answers a run waiting for input; run_status and run_log read a run; approvals lists the steps "
    "waiting for the person; model_deploy deploys a commit of a model's GitHub repo; audit_query reads the audit log "
    "(Enterprise); cluster_status shows the runtime's cluster."
)


class RpcError(Exception):
    def __init__(self, code: int, message: str, status: int = 200) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[Services, dict[str, Any]], Awaitable[dict[str, Any]]]
    read_only: bool = False


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


async def models_list(s: Services, _: dict[str, Any]) -> dict[str, Any]:
    return {"models": s.models.describe()}


async def model_call(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.call(s, args)


async def model_continue(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.resume(s, ops.run_id_of(args), args.get("answer"))


async def model_cancel(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.cancel(s, ops.run_id_of(args))


async def run_status(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.run_status(s, ops.run_id_of(args))


async def run_log(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.run_log(s, ops.run_id_of(args))


async def approvals(s: Services, _: dict[str, Any]) -> dict[str, Any]:
    return {"approvals": await ops.approvals(s)}


async def model_deploy(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.deploy(s, args)


async def audit_query(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    from rrotord.api.routes import audited

    return await audited(s, args)


async def cluster_status(s: Services, _: dict[str, Any]) -> dict[str, Any]:
    from rrotord.api.routes import clustered

    return clustered(s)


RUN = _obj({"run_id": {"type": "string"}}, ["run_id"])
TOOLS: tuple[Tool, ...] = (
    Tool(
        "models_list",
        "The models here: each one's versions, the active one and its functions.",
        _obj({}),
        models_list,
        True,
    ),
    Tool(
        "model_call",
        "Run a function of a model: the first Result, with its data. Waits for the model.",
        _obj(
            {
                "model": {"type": "string"},
                "function": {"type": "string", "default": "start"},
                "inputs": {"type": "object"},
                "version": {"type": "integer", "minimum": 1},
                "clearance": {"type": "number", "minimum": 0, "maximum": 1},
            },
            ["model"],
        ),
        model_call,
    ),
    Tool(
        "model_continue",
        "Answer a run that is waiting for input or an approval: the next Result.",
        _obj({"run_id": {"type": "string"}, "answer": {}}, ["run_id"]),
        model_continue,
    ),
    Tool("model_cancel", "Stop a run; its end runs.", RUN, model_cancel),
    Tool("run_status", "A run's status, timing and last Result.", RUN, run_status, True),
    Tool("run_log", "A run's log lines.", RUN, run_log, True),
    Tool(
        "approvals", "The runs waiting for the person's approval: run, model, step, prompt.", _obj({}), approvals, True
    ),
    Tool(
        "model_deploy",
        "Deploy a commit of a model's GitHub repo (owner/name) as its next version; a token for a private repo.",
        _obj(
            {"repo": {"type": "string"}, "commit": {"type": "string"}, "token": {"type": "string"}}, ["repo", "commit"]
        ),
        model_deploy,
    ),
    Tool(
        "audit_query",
        "The audit log, newest first: who called what and how it ended, deploys, key and licence changes (Enterprise).",
        _obj(
            {
                "since": {"type": "string", "format": "date-time"},
                "until": {"type": "string", "format": "date-time"},
                "kind": {"type": "string"},
                "model": {"type": "string"},
                "run_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            }
        ),
        audit_query,
        True,
    ),
    Tool(
        "cluster_status",
        "This runtime's cluster as this node sees it: its id, the head, the term, the members and the nodes refused; "
        "a standalone node says so.",
        _obj({}),
        cluster_status,
        True,
    ),
)
BY_NAME = {t.name: t for t in TOOLS}


def listed(tool: Tool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "title": tool.name.replace("_", " ").capitalize(),
        "description": tool.description,
        "inputSchema": tool.schema,
        "annotations": {"readOnlyHint": tool.read_only, "openWorldHint": False},
    }


def server_info() -> dict[str, str]:
    return {"name": "rrotor", "title": "Glyphh runtime", "version": VERSION}


def read(raw: bytes) -> tuple[dict[str, Any], str]:
    """One JSON-RPC message and the revision it speaks (MODERN when its _meta says so, else the oldest legacy)."""
    try:
        msg = json.loads(raw)
    except ValueError as err:
        raise RpcError(PARSE_ERROR, "The body is not JSON.", 400) from err
    ok = isinstance(msg, dict) and msg.get("jsonrpc") == "2.0" and isinstance(msg.get("method"), str)
    if not ok or not isinstance(msg.get("params", {}), dict):
        raise RpcError(INVALID_REQUEST, "One JSON-RPC 2.0 request or notification per POST.", 400)
    if "id" in msg and (isinstance(msg["id"], bool) or not isinstance(msg["id"], str | int)):
        raise RpcError(INVALID_REQUEST, "A request's id is a string or an integer.", 400)
    meta = msg.get("params", {}).get("_meta")
    version = meta.get(VERSION_META) if isinstance(meta, dict) else None
    return msg, MODERN if version == MODERN else LEGACY[-1]


async def tool_call(s: Services, params: dict[str, Any]) -> dict[str, Any]:
    tool = BY_NAME.get(str(params.get("name", "")))
    if tool is None:
        raise RpcError(INVALID_PARAMS, f"There is no tool {params.get('name')!r}.")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        raise RpcError(INVALID_PARAMS, "arguments is an object.")
    try:
        data = await tool.handler(s, args)
    except RrotorError as err:
        body = err.to_json()
        return {"content": [{"type": "text", "text": err.message}], "structuredContent": body, "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(data)}], "structuredContent": data, "isError": False}


async def handle(s: Services, msg: dict[str, Any], version: str) -> dict[str, Any]:
    params = msg.get("params", {})
    match msg["method"]:
        case "initialize":
            if version == MODERN:
                raise RpcError(METHOD_NOT_FOUND, "initialize is not part of MCP 2026-07-28: use server/discover.", 404)
            asked = params.get("protocolVersion")
            return {
                "protocolVersion": asked if asked in LEGACY else LEGACY[0],
                "capabilities": {"tools": {}},
                "serverInfo": server_info(),
                "instructions": INSTRUCTIONS,
            }
        case "ping":
            return {}
        case "server/discover":
            return {"supportedVersions": [MODERN, *LEGACY], "capabilities": {"tools": {}}, "instructions": INSTRUCTIONS}
        case "tools/list":
            return {"tools": [listed(t) for t in TOOLS]}
        case "tools/call":
            return await tool_call(s, params)
    raise RpcError(METHOD_NOT_FOUND, f"There is no method {msg['method']!r}.", 404 if version == MODERN else 200)


def failure(err: RpcError, rpc_id: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "error": {"code": err.code, "message": err.message}}
    if rpc_id is not None:
        body["id"] = rpc_id
    return body


def route(s: Services) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    async def mcp(request: web.Request) -> web.StreamResponse:
        try:
            msg, version = read(await request.read())
        except RpcError as err:
            return web.json_response(failure(err), status=err.status)
        if "id" not in msg:
            return web.Response(status=202)
        try:
            result = await handle(s, msg, version)
        except RpcError as err:
            return web.json_response(failure(err, msg["id"]), status=err.status)
        if version == MODERN:
            result = {
                **result,
                "resultType": "complete",
                "_meta": {"io.modelcontextprotocol/serverInfo": server_info()},
            }
        return web.json_response({"jsonrpc": "2.0", "id": msg["id"], "result": result})

    return mcp
