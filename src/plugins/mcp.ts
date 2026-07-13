/**
 * A generic MCP (Model Context Protocol) client over **streamable HTTP** and the
 * connection registry that exposes a remote server's tools to rotors (SPEC §7.16,
 * §8.3). It speaks JSON-RPC 2.0 — `initialize` → `tools/list` → `tools/call` — and
 * registers each discovered tool into a {@link ConnectionsPlugin}, namespaced by
 * connection id, so a `tool.mcp` step dispatches straight to it.
 *
 * This module is deliberately **provider-agnostic**. Auth is an injected async
 * `headers` provider, called per request, so a host (e.g. glyphh-server, which
 * fronts Pipedream Connect) can mint a fresh short-lived access token and add the
 * per-user context (`x-pd-external-user-id`, app slug) on every call. OpenRotor
 * knows nothing about any specific provider — it only knows how to speak MCP.
 *
 * Determinism (§6.2): a `tool.mcp` call is a nondeterministic side effect and is
 * checkpointed in the StepRecord by the executor, so replay returns the recorded
 * result and NEVER re-dials the server (or re-mints a token).
 */

import { VERSION } from "../version.js";
import { RotorError } from "../errors.js";
import type { ConnectionsPlugin, Row, ToolSchema } from "./interfaces.js";

/** Static headers, or a (possibly async) provider called per request. */
export type HeaderProvider =
  | Record<string, string>
  | (() => Record<string, string> | Promise<Record<string, string>>);

export interface McpConnection {
  /** Namespace for this connection's tools, e.g. `pipedream` or `myserver`. */
  id: string;
  /** The MCP endpoint URL (streamable HTTP). */
  url: string;
  /** Convenience static bearer token for simple servers. */
  token?: string;
  /** Per-request headers — mint tokens / inject per-user context here. */
  headers?: HeaderProvider;
  kind?: "mcp" | "provider";
}

export interface McpToolResult {
  content: unknown;
  /** Concatenated text blocks — the common `tool` output. */
  text: string;
  isError: boolean;
}

interface RpcResponse {
  result?: Record<string, unknown>;
  error?: { code: number; message: string };
}

async function resolveHeaders(conn: McpConnection): Promise<Record<string, string>> {
  const h: Record<string, string> = {
    "content-type": "application/json",
    accept: "application/json, text/event-stream",
  };
  if (conn.token) h.authorization = `Bearer ${conn.token}`;
  if (typeof conn.headers === "function") Object.assign(h, await conn.headers());
  else if (conn.headers) Object.assign(h, conn.headers);
  return h;
}

/** Parse a JSON-RPC response from either a JSON body or an SSE `data:` frame. */
async function parseRpc(res: Response): Promise<RpcResponse> {
  const body = await res.text();
  const ct = res.headers.get("content-type") ?? "";
  if (ct.includes("text/event-stream")) {
    for (const line of body.split(/\r?\n/)) {
      const m = /^data:\s*(.+)$/.exec(line.trim());
      if (m) {
        try {
          return JSON.parse(m[1]) as RpcResponse;
        } catch {
          /* keep scanning */
        }
      }
    }
    throw new RotorError("E_TRANSPORT", "MCP: no JSON-RPC frame in SSE response");
  }
  return JSON.parse(body) as RpcResponse;
}

export class McpClient {
  private nextId = 0;
  private sessionId?: string;

  constructor(private readonly conn: McpConnection) {}

  private async rpc(method: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
    const headers = await resolveHeaders(this.conn);
    if (this.sessionId) headers["mcp-session-id"] = this.sessionId;
    const res = await fetch(this.conn.url, {
      method: "POST",
      headers,
      body: JSON.stringify({ jsonrpc: "2.0", id: ++this.nextId, method, params }),
    });
    const sid = res.headers.get("mcp-session-id");
    if (sid) this.sessionId = sid;
    if (!res.ok) throw new RotorError("E_TRANSPORT", `MCP ${method} → HTTP ${res.status}`, { context: { method, status: res.status } });
    const rpc = await parseRpc(res);
    if (rpc.error) throw new RotorError("E_TOOL", `MCP ${method}: ${rpc.error.message}`, { context: { method } });
    return rpc.result ?? {};
  }

  /** Handshake: announce the client and negotiate capabilities. */
  async initialize(): Promise<Record<string, unknown>> {
    return this.rpc("initialize", {
      protocolVersion: "2025-03-26",
      capabilities: {},
      clientInfo: { name: "openrotor", version: VERSION },
    });
  }

  /** Discover the server's tools. */
  async listTools(): Promise<ToolSchema[]> {
    const r = await this.rpc("tools/list", {});
    const tools = (r.tools ?? []) as Array<{ name: string; description?: string; inputSchema?: unknown }>;
    return tools.map((t) => ({ name: t.name, description: t.description, inputSchema: t.inputSchema }));
  }

  /** Invoke a tool; normalize the MCP content into a `tool`-friendly result. */
  async callTool(name: string, args: Row): Promise<McpToolResult> {
    const r = await this.rpc("tools/call", { name, arguments: args });
    const content = (r.content ?? []) as Array<{ type?: string; text?: string }>;
    const text = content
      .filter((c) => c.type === "text" && typeof c.text === "string")
      .map((c) => c.text)
      .join("\n");
    return { content: r.content ?? [], text, isError: r.isError === true };
  }
}

export interface ConnectMcpResult {
  registered: string[];
  client: McpClient;
}

/**
 * Connect to an MCP server, discover its tools, and register each into the
 * `connections` registry as `<connection.id>:<tool.name>` so a `tool.mcp` step can
 * dispatch to it. Returns the registered method names + the live client.
 */
export async function connectMcp(connections: ConnectionsPlugin, conn: McpConnection): Promise<ConnectMcpResult> {
  const client = new McpClient(conn);
  await client.initialize();
  const tools = await client.listTools();
  const registered: string[] = [];
  for (const t of tools) {
    const method = `${conn.id}:${t.name}`;
    connections.register(method, async (args: Row) => client.callTool(t.name, args), {
      name: method,
      description: t.description,
      inputSchema: t.inputSchema,
    });
    registered.push(method);
  }
  return { registered, client };
}
