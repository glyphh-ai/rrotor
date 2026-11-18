"""The runtime's own MCP server, `POST /mcp` (Streamable HTTP, stateless, one JSON-RPC message per POST): Claude adds
the runtime as a connector with a runtime key and talks to it directly. MCP 2026-07-28 (`server/discover`, each
request's _meta naming the version) and the earlier initialize revisions are both served. The tools are the API's
operations (ops.py), so REST and MCP do the same thing. Its resources are its panels (panel.py): the secrets form,
an MCP App `secrets_form` names, where a person types a secret's value so that it never passes through the chat."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiohttp import web

from rrotord import VERSION
from rrotord.api import ops, panel
from rrotord.errors import RrotorError

if TYPE_CHECKING:
    from rrotord.api.routes import Services

MODERN = "2026-07-28"
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26")
VERSION_META = "io.modelcontextprotocol/protocolVersion"
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
INSTRUCTIONS = (
    "This person's Glyphh runtime (rrotor): their programs, deployed by commit and run here. programs_list shows them; "
    "program_call runs a function and answers its Result (done, needs_input, needs_approval or failed, with data); "
    "program_continue answers a run waiting for input; run_status and run_log read a run; approvals lists the steps "
    "waiting for the person; program_deploy deploys a commit of a program's GitHub repo; audit_query reads the audit "
    "log (Enterprise); cluster_status shows the runtime's cluster; secrets_form shows the person a form to set a "
    "program's secrets themselves (call it whenever a program needs a secret set, and never ask for a secret's value "
    "in the chat); secrets_list, secret_set and secret_delete keep them (values are never shown back); "
    "schedules_list, schedule_pause and schedule_resume show and "
    "pause the schedules this runtime fires itself. clis_list shows the command-line tools installed here, and "
    "cli_run runs one command of one in the sandbox (args checked against its allow patterns; one that needs approval "
    "answers needs_approval, then program_continue runs it once the person approves). The vendors' agent CLIs (claude, "
    "codex, gemini, cursor, grok, copilot) and glyphh come built in; cli_login signs one in from the person's phone: "
    "it answers needs_input with an address (and a device code) to open, and program_continue brings back the code "
    "the page shows. A program runs a CLI it names in its clis with ctx.cli. app_publish puts what Claude made (a "
    "Claude Code artifact, .html or .md; or a React page, a Markdown document, a diagram, a PDF or an image) on this "
    "runtime as an app for the org's people, reviewed in Glyphh's catalogue before it is served at /apps/<name>/; "
    "apps_list shows the apps here, each one's type and its review state. The graph is this runtime's semantic "
    "layer, one knowledge graph of the org's data: nodes (a type and a key, with a title, props and text) and edges "
    'between them. A pipeline is a program on a schedule whose glyphh.json says "graph": "write": it reads a '
    "source with its secrets and writes nodes and edges with ctx.graph. graph_schema shows what the graph holds (read "
    "it before writing a pipeline or an app); graph_search finds nodes by their words and answers the passages to "
    "answer from; graph_node, graph_nodes and graph_neighbours read it; graph_upsert and graph_delete write it "
    'directly. An app whose functions read it says "graph": "read" and uses ctx.graph.'
)
# MCP 2026-07-28: these results carry ttlMs and cacheScope (a client's SDK refuses a resources/read without them);
# never reused, since what they hold changes.
CACHEABLE = frozenset({"tools/list", "resources/list", "resources/templates/list", "resources/read"})
NO_CACHE: dict[str, Any] = {"ttlMs": 0, "cacheScope": "private"}
CAPABILITIES: dict[str, Any] = {"tools": {}, "resources": {}}


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
    panel: str | None = None  # the MCP App its answer is shown in (api/panel.py)


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


async def programs_list(s: Services, _: dict[str, Any]) -> dict[str, Any]:
    return {"programs": s.programs.describe()}


async def program_call(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.call(s, args)


async def program_continue(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.resume(s, ops.run_id_of(args), args.get("answer"))


async def program_cancel(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.cancel(s, ops.run_id_of(args))


async def run_status(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.run_status(s, ops.run_id_of(args))


async def run_log(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.run_log(s, ops.run_id_of(args))


async def approvals(s: Services, _: dict[str, Any]) -> dict[str, Any]:
    return {"approvals": await ops.approvals(s)}


async def program_deploy(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.deploy(s, args)


async def audit_query(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    from rrotord.api.routes import audited

    return await audited(s, args)


async def cluster_status(s: Services, _: dict[str, Any]) -> dict[str, Any]:
    from rrotord.api.routes import clustered

    return clustered(s)


async def secrets_list(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.secrets_list(s, ops.text_of(args, "program"))


async def secret_set(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.secret_set(s, ops.text_of(args, "program"), ops.text_of(args, "name"), args.get("value"))


async def secret_delete(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.secret_delete(s, ops.text_of(args, "program"), ops.text_of(args, "name"))


async def secrets_form(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.secrets_form(s, ops.text_of(args, "program"))


def graph_tool(op: str) -> Callable[[Services, dict[str, Any]], Awaitable[dict[str, Any]]]:
    async def handler(s: Services, args: dict[str, Any]) -> dict[str, Any]:
        return await ops.graph(s, op, args)

    return handler


async def clis_list(s: Services, _: dict[str, Any]) -> dict[str, Any]:
    return await ops.clis_list(s)


async def cli_run(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.cli_run(s, args)


async def cli_login(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.cli_login(s, {"cli": ops.text_of(args, "cli")})


async def apps_list(s: Services, _: dict[str, Any]) -> dict[str, Any]:
    return await ops.apps_list(s)


async def app_publish(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.app_publish(s, args)


async def schedules_list(s: Services, _: dict[str, Any]) -> dict[str, Any]:
    return await ops.schedules(s)


async def schedule_pause(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.schedule_pause(s, ops.text_of(args, "program"), ops.text_of(args, "name"), True)


async def schedule_resume(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return await ops.schedule_pause(s, ops.text_of(args, "program"), ops.text_of(args, "name"), False)


NODE_REF = _obj({"type": {"type": "string"}, "key": {"type": "string"}}, ["type", "key"])
NODE = {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "key": {"type": "string"},
        "title": {"type": "string"},
        "props": {"type": "object"},
        "text": {"type": "string"},
    },
    "required": ["type", "key"],
}
EDGE = {
    "type": "object",
    "properties": {"from": NODE_REF, "rel": {"type": "string"}, "to": NODE_REF, "props": {"type": "object"}},
    "required": ["from", "rel", "to"],
}
PAGE = {"type": "integer", "minimum": 1, "maximum": 200}
GRAPH_TOOLS: tuple[Tool, ...] = (
    Tool(
        "graph_schema",
        "What this runtime's graph holds: each type of node (how many, the props its nodes carry, the programs that "
        "wrote them), each relation between two types (how many), and the totals. Read it before writing a pipeline, "
        "an app's functions or a search.",
        _obj({}),
        graph_tool("schema"),
        True,
    ),
    Tool(
        "graph_search",
        "Search the graph by words: the nodes whose title, props or text answer the query, the best first (BM25), "
        "each with the passage that matched (the text to answer from), a snippet and its score; types narrows it to "
        "some types of node; expand adds the nodes each hit is linked to (out, in).",
        _obj(
            {
                "query": {"type": "string"},
                "types": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "expand": {"type": "boolean"},
            },
            ["query"],
        ),
        graph_tool("search"),
        True,
    ),
    Tool(
        "graph_node",
        "One node of the graph, whole: its title, props, text, the program that wrote it and when, and its edges "
        "each way (out, in) with the nodes they reach.",
        NODE_REF,
        graph_tool("node"),
        True,
    ),
    Tool(
        "graph_nodes",
        "The nodes of one type, the newest first: those whose props equal every value in where ({prop: value}); "
        "total counts them all.",
        _obj(
            {
                "type": {"type": "string"},
                "where": {"type": "object"},
                "limit": PAGE,
                "offset": {"type": "integer", "minimum": 0},
            },
            ["type"],
        ),
        graph_tool("nodes"),
        True,
    ),
    Tool(
        "graph_neighbours",
        "The graph around a node: the nodes within depth edges of it (1 to 3), each with its distance, and the edges "
        "among them; rel follows one relation alone, direction out, in or both.",
        _obj(
            {
                "type": {"type": "string"},
                "key": {"type": "string"},
                "rel": {"type": "string"},
                "direction": {"type": "string", "enum": ["out", "in", "both"]},
                "depth": {"type": "integer", "minimum": 1, "maximum": 3},
                "limit": PAGE,
            },
            ["type", "key"],
        ),
        graph_tool("neighbours"),
        True,
    ),
    Tool(
        "graph_upsert",
        "Write nodes and edges to the graph, each made or changed, all of them or none: a node is {type, key, title?, "
        "props?, text?} (type: lower-case, digits, _; written again, it keeps what this write does not name), an edge "
        "{from: {type, key}, rel, to: {type, key}, props?} (an end not written yet is made, empty). At most 2,000 "
        'nodes and 10,000 edges a call. For data that keeps changing, write a pipeline (a program with "graph": '
        '"write" on a schedule) instead.',
        _obj({"nodes": {"type": "array", "items": NODE}, "edges": {"type": "array", "items": EDGE}}),
        graph_tool("upsert"),
    ),
    Tool(
        "graph_delete",
        "Remove nodes ({type, key}, with their edges) and edges ({from, rel, to}) from the graph: how many were there.",
        _obj({"nodes": {"type": "array", "items": NODE_REF}, "edges": {"type": "array", "items": EDGE}}),
        graph_tool("delete"),
    ),
)
RUN = _obj({"run_id": {"type": "string"}}, ["run_id"])
SCHEDULE_ARGS = _obj({"program": {"type": "string"}, "name": {"type": "string"}}, ["program", "name"])
FILES = {
    "type": "object",
    "additionalProperties": {"anyOf": [{"type": "string"}, _obj({"base64": {"type": "string"}}, ["base64"])]},
}
TOOLS: tuple[Tool, ...] = (
    Tool(
        "apps_list",
        "The apps here (what Claude made, published for the org's people): each one's title, type (page, dashboard, "
        "doc, diagram, deck, design), version, roles, the catalogue's review state (draft, approved, rejected, "
        "retired, unknown) and the address a browser opens it at.",
        _obj({}),
        apps_list,
        True,
    ),
    Tool(
        "app_publish",
        "Publish what Claude made (a Claude Code artifact: one .html or .md file and any files beside it; or a React "
        "page (.jsx, .tsx), a Markdown document, a diagram (.mmd, .svg), a PDF or an image) to this runtime as an app "
        "for the org's people: name (the program's name: lower-case, digits, _), title, description, type (page, "
        "dashboard, doc, diagram, deck, design; default by the entry: a page, .md and .pdf a doc, .mmd and .svg a "
        "diagram, an image a design; say dashboard or deck yourself), roles (groups, admin, member; empty: everyone in "
        "the org), files (path to text or {base64} for a PDF or an image; 64 and 16 MiB at most), entry (default "
        "index.html, or the one file sent), and, when the page calls functions, functions ({name: {description, "
        "inputs}}) with program (the text of program.py; a function runs as the viewer, with their CLIs; graph: "
        '"read" lets its functions read this runtime\'s graph with ctx.graph). The app '
        "awaits the org's review in Glyphh (an admin or a reviewer approves it), then is served at the url answered. "
        "A new version of an approved app keeps the approved one serving until it is approved.",
        _obj(
            {
                "name": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "type": {"type": "string", "enum": ["page", "dashboard", "doc", "diagram", "deck", "design"]},
                "roles": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
                "files": FILES,
                "entry": {"type": "string"},
                "functions": {"type": "object", "additionalProperties": {"type": "object"}},
                "program": {"type": "string"},
                "hosts": {"type": "array", "items": {"type": "string"}},
                "secrets": {"type": "array", "items": {"type": "string"}},
                "clis": {"type": "array", "items": {"type": "string"}},
                "graph": {"type": "string", "enum": ["read", "write"]},
                "limits": {"type": "object"},
            },
            ["name", "title", "files"],
        ),
        app_publish,
    ),
    Tool(
        "programs_list",
        "The programs here: each one's versions, the active one and its functions.",
        _obj({}),
        programs_list,
        True,
    ),
    Tool(
        "program_call",
        "Run a function of a program: the first Result, with its data. Waits for the program.",
        _obj(
            {
                "program": {"type": "string"},
                "function": {"type": "string", "default": "start"},
                "inputs": {"type": "object"},
                "version": {"type": "integer", "minimum": 1},
                "clearance": {"type": "number", "minimum": 0, "maximum": 1},
            },
            ["program"],
        ),
        program_call,
    ),
    Tool(
        "program_continue",
        "Answer a run that is waiting for input or an approval: the next Result.",
        _obj({"run_id": {"type": "string"}, "answer": {}}, ["run_id"]),
        program_continue,
    ),
    Tool("program_cancel", "Stop a run; its end runs.", RUN, program_cancel),
    Tool("run_status", "A run's status, timing and last Result.", RUN, run_status, True),
    Tool("run_log", "A run's log lines.", RUN, run_log, True),
    Tool(
        "approvals",
        "The runs waiting for the person's approval: run, program, step, prompt.",
        _obj({}),
        approvals,
        True,
    ),
    Tool(
        "program_deploy",
        "Deploy a commit of a program's GitHub repo (owner/name) as its next version; a token for a private repo.",
        _obj(
            {"repo": {"type": "string"}, "commit": {"type": "string"}, "token": {"type": "string"}}, ["repo", "commit"]
        ),
        program_deploy,
    ),
    Tool(
        "audit_query",
        "The audit log, newest first: who called what and how it ended, deploys, key and licence changes (Enterprise).",
        _obj(
            {
                "since": {"type": "string", "format": "date-time"},
                "until": {"type": "string", "format": "date-time"},
                "kind": {"type": "string"},
                "program": {"type": "string"},
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
    Tool(
        "secrets_list",
        "A program's secrets by name: whether its active version declares each and whether it is set. Never a value.",
        _obj({"program": {"type": "string"}}, ["program"]),
        secrets_list,
        True,
    ),
    Tool(
        "secrets_form",
        "Show the person a form to set a program's secrets themselves: each name its glyphh.json declares, whether it "
        "is set, and a field for its value, which goes straight to this runtime's vault and never into the chat. Call "
        "this whenever a program needs a secret set (a run failed E_SECRET_MISSING, or a pipeline was just deployed); "
        "never ask the person to paste a secret's value into the chat. Answers the names and which are unset.",
        _obj({"program": {"type": "string"}}, ["program"]),
        secrets_form,
        True,
        panel.SECRETS_URI,
    ),
    Tool(
        "secret_set",
        "Set a program's secret (a name its glyphh.json declares), kept encrypted on this runtime; never shown back. "
        "The secrets form calls this with what the person typed; do not ask for a value in the chat: use secrets_form.",
        _obj(
            {"program": {"type": "string"}, "name": {"type": "string"}, "value": {"type": "string"}},
            ["program", "name", "value"],
        ),
        secret_set,
    ),
    Tool(
        "secret_delete",
        "Delete a program's secret.",
        SCHEDULE_ARGS,
        secret_delete,
    ),
    Tool(
        "schedules_list",
        "The programs' schedules: function, cron and zone, the next fire, the last run and skip, paused or not.",
        _obj({}),
        schedules_list,
        True,
    ),
    Tool(
        "schedule_pause",
        "Pause a schedule (by program and name: the function's, unless it names itself); kept across restarts.",
        SCHEDULE_ARGS,
        schedule_pause,
    ),
    Tool("schedule_resume", "Resume a paused schedule.", SCHEDULE_ARGS, schedule_resume),
    Tool(
        "clis_list",
        "The command-line tools here: each one's version, command, install, allowed argument patterns, the "
        "patterns that need the person's approval, and signed_in: whether it is signed in for you (each signed-in "
        "person has their own HOME on the rrotor; false: call cli_login first).",
        _obj({}),
        clis_list,
        True,
    ),
    Tool(
        "cli_run",
        "Run one command of a CLI in the sandbox (no shell): its exit code, stdout and stderr (64 KB each, the rest "
        "in run_log) and the files it made or changed in its working folder; needs_approval when its arguments need "
        "the person's approval. files: path to text, or {base64}.",
        _obj(
            {
                "cli": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}, "maxItems": 256},
                "stdin": {"type": "string"},
                "files": {
                    "type": "object",
                    "additionalProperties": {
                        "anyOf": [
                            {"type": "string"},
                            _obj({"base64": {"type": "string"}}, ["base64"]),
                        ]
                    },
                },
            },
            ["cli", "args"],
        ),
        cli_run,
    ),
    Tool(
        "cli_login",
        "Sign a CLI in from afar (one whose clis_list entry has login), its sign-in kept in your own private HOME "
        "on the rrotor (each signed-in person signs in as themselves) for later cli_run calls: answers needs_input "
        "with data.url (and data.code for a device flow) for the person to open on their phone; then "
        "program_continue with the code the page shows (or, if the page ends on an http://localhost… address that "
        "does not load, that address; after a device code, any answer once they signed in). Answers done with "
        "signed_in when it has.",
        _obj({"cli": {"type": "string"}}, ["cli"]),
        cli_login,
    ),
    *GRAPH_TOOLS,
)
BY_NAME = {t.name: t for t in TOOLS}


def listed(tool: Tool) -> dict[str, Any]:
    shown: dict[str, Any] = {
        "name": tool.name,
        "title": tool.name.replace("_", " ").capitalize(),
        "description": tool.description,
        "inputSchema": tool.schema,
        "annotations": {"readOnlyHint": tool.read_only, "openWorldHint": False},
    }
    if tool.panel is not None:  # a client without panels ignores it
        shown["_meta"] = {"ui": {"resourceUri": tool.panel}}
    return shown


def read_resource(params: dict[str, Any]) -> dict[str, Any]:
    uri = params.get("uri")
    if uri not in panel.PAGES:
        raise RpcError(INVALID_PARAMS, f"There is no resource {uri!r}.")
    return {"contents": [panel.contents(uri)]}


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
                "capabilities": CAPABILITIES,
                "serverInfo": server_info(),
                "instructions": INSTRUCTIONS,
            }
        case "ping":
            return {}
        case "server/discover":
            return {"supportedVersions": [MODERN, *LEGACY], "capabilities": CAPABILITIES, "instructions": INSTRUCTIONS}
        case "tools/list":
            return {"tools": [listed(t) for t in TOOLS]}
        case "tools/call":
            return await tool_call(s, params)
        case "resources/list":
            return {"resources": list(panel.RESOURCES)}
        case "resources/templates/list":
            return {"resourceTemplates": []}
        case "resources/read":
            return read_resource(params)
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
                **(NO_CACHE if msg["method"] in CACHEABLE else {}),
                "resultType": "complete",
                "_meta": {"io.modelcontextprotocol/serverInfo": server_info()},
            }
        return web.json_response({"jsonrpc": "2.0", "id": msg["id"], "result": result})

    return mcp
