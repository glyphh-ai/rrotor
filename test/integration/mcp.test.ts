/**
 * MCP connection integration (the Pipedream/custom-MCP path). A mock MCP server
 * over streamable HTTP validates: tool discovery + namespaced registration, tool
 * invocation, auth header injection (static token AND an async per-user provider,
 * the Pipedream Connect pattern), and that a `tool.mcp` result is checkpointed so
 * replay never re-dials the server.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import * as http from "node:http";
import type { AddressInfo } from "node:net";

import { execute } from "../../src/exec/executor.js";
import { buildBasicPlugins, connectMcp } from "../../src/plugins/index.js";
import { InProcessStore } from "../../src/exec/store.js";
import type { RotorDocument } from "../../src/types.js";

let server: http.Server;
let url: string;
let calls: { list: number; call: number };
let lastHeaders: http.IncomingHttpHeaders;

beforeAll(async () => {
  calls = { list: 0, call: 0 };
  server = http.createServer((req, res) => {
    lastHeaders = req.headers;
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const rpc = JSON.parse(body || "{}") as { id: number; method: string; params?: { name?: string; arguments?: Record<string, unknown> } };
      const reply = (result: unknown) => {
        res.writeHead(200, { "content-type": "application/json", "mcp-session-id": "sess-1" });
        res.end(JSON.stringify({ jsonrpc: "2.0", id: rpc.id, result }));
      };
      if (rpc.method === "initialize") return reply({ protocolVersion: "2025-03-26", capabilities: { tools: {} }, serverInfo: { name: "mock", version: "1" } });
      if (rpc.method === "tools/list") {
        calls.list++;
        return reply({
          tools: [
            { name: "add", description: "add two numbers", inputSchema: { type: "object" } },
            { name: "echo", description: "echo the args", inputSchema: { type: "object" } },
          ],
        });
      }
      if (rpc.method === "tools/call") {
        calls.call++;
        const a = rpc.params?.arguments ?? {};
        if (rpc.params?.name === "add") return reply({ content: [{ type: "text", text: String(Number(a.a) + Number(a.b)) }], isError: false });
        return reply({ content: [{ type: "text", text: JSON.stringify(a) }], isError: false });
      }
      reply({});
    });
  });
  await new Promise<void>((r) => server.listen(0, () => r()));
  url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});

afterAll(async () => {
  await new Promise<void>((r) => server.close(() => r()));
});

describe("MCP discovery + dispatch", () => {
  it("registers the server's tools namespaced by connection id", async () => {
    const plugins = buildBasicPlugins();
    const { registered } = await connectMcp(plugins.connections, { id: "mock", url });
    expect(registered).toEqual(["mock:add", "mock:echo"]);
    expect(plugins.connections.listTools().map((t) => t.name)).toEqual(expect.arrayContaining(["mock:add", "mock:echo"]));
  });

  it("dispatches a call to the remote tool and normalizes the result", async () => {
    const plugins = buildBasicPlugins();
    await connectMcp(plugins.connections, { id: "mock", url });
    const res = await plugins.connections.dispatch("mock:add", { a: 2, b: 3 });
    expect(res).toMatchObject({ ok: true });
    expect((res as { ok: true; result: { text: string } }).result.text).toBe("5");
  });
});

describe("auth injection", () => {
  it("sends a static bearer token", async () => {
    const plugins = buildBasicPlugins();
    await connectMcp(plugins.connections, { id: "mock", url, token: "secret-123" });
    expect(lastHeaders.authorization).toBe("Bearer secret-123");
  });

  it("calls an async headers provider per request (Pipedream Connect pattern)", async () => {
    const plugins = buildBasicPlugins();
    // The provider mints/injects per-user context — here the external user id.
    await connectMcp(plugins.connections, {
      id: "pd",
      url,
      headers: async () => ({ "x-pd-external-user-id": "user-42", "x-pd-app-slug": "gmail" }),
    });
    await plugins.connections.dispatch("pd:echo", { hi: true });
    expect(lastHeaders["x-pd-external-user-id"]).toBe("user-42");
    expect(lastHeaders["x-pd-app-slug"]).toBe("gmail");
  });
});

describe("SSE (text/event-stream) responses", () => {
  it("parses a JSON-RPC frame delivered as SSE (streamable HTTP)", async () => {
    const sse = http.createServer((req, res) => {
      let body = "";
      req.on("data", (c) => (body += c));
      req.on("end", () => {
        const rpc = JSON.parse(body || "{}") as { id: number; method: string; params?: { name?: string; arguments?: Record<string, unknown> } };
        const send = (result: unknown) => {
          res.writeHead(200, { "content-type": "text/event-stream" });
          res.end(`event: message\ndata: ${JSON.stringify({ jsonrpc: "2.0", id: rpc.id, result })}\n\n`);
        };
        if (rpc.method === "initialize") return send({ protocolVersion: "2025-03-26", capabilities: {}, serverInfo: { name: "sse", version: "1" } });
        if (rpc.method === "tools/list") return send({ tools: [{ name: "ping", inputSchema: { type: "object" } }] });
        return send({ content: [{ type: "text", text: "pong" }], isError: false });
      });
    });
    await new Promise<void>((r) => sse.listen(0, () => r()));
    const sseUrl = `http://127.0.0.1:${(sse.address() as AddressInfo).port}`;

    const plugins = buildBasicPlugins();
    const { registered } = await connectMcp(plugins.connections, { id: "sse", url: sseUrl });
    expect(registered).toEqual(["sse:ping"]);
    const res = await plugins.connections.dispatch("sse:ping", {});
    expect((res as { ok: true; result: { text: string } }).result.text).toBe("pong");
    await new Promise<void>((r) => sse.close(() => r()));
  });
});

describe("determinism: MCP tool results are checkpointed", () => {
  const doc: RotorDocument = {
    apiVersion: "rotor.glyphh.ai/v0.1",
    kind: "Rotor",
    metadata: { name: "mcp", version: "0.1.0" },
    spec: { entry: "t", steps: [{ id: "t", type: "tool", in: {}, out: {}, config: { flavor: "mcp", name: "mock:add", args: { a: 2, b: 3 } }, next: "end" }] },
  } as unknown as RotorDocument;

  it("replays from the record without re-dialing the server", async () => {
    const store = new InProcessStore();
    const before = calls.call;

    const p1 = buildBasicPlugins({ store });
    await connectMcp(p1.connections, { id: "mock", url });
    const r1 = await execute(doc, {}, p1);
    expect((r1.history.find((h) => h.step_id === "t")?.output as { result: { text: string } }).result.text).toBe("5");
    expect(calls.call).toBe(before + 1); // dialed once

    // Replay against the shared store — the tool step returns its recorded result.
    const p2 = buildBasicPlugins({ store });
    const r2 = await execute(doc, {}, p2);
    expect(r2.status).toBe("ok");
    expect(calls.call).toBe(before + 1); // NOT re-dialed
  });
});
