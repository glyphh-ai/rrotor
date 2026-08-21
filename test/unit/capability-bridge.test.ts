/**
 * capability-bridge.test.ts — the REAL host side of `glyphh.call` on a pod
 * (src/app-worker/capability-bridge.ts), exercised against a MOCK control
 * plane (a real node:http server, the app-bundle.test.ts pattern) speaking
 * the server's `{ data }` / `{ error }` envelopes.
 *
 * The contract: manifest grant enforced BEFORE dispatch; the data-plane
 * families (data.query / vector.* / graph.*) ride the `gy_wk_` worker token
 * to the app's own surfaces with desktop-compatible arg/result shapes;
 * connector.* stays scoped to manifest-declared connectors and surfaces the
 * server's refusals with the capability attached; cron.* lands on the durable
 * scheduler; every desktop-only capability is refused with ONE precise
 * message shape; and a worker script's `glyphh.call` reaches all of it
 * through the executor end to end.
 */

import { describe, it, expect, beforeAll, afterAll, beforeEach, afterEach } from "vitest";
import { createServer, type Server } from "node:http";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  createCapabilityBridge,
  executorBridge,
  singleAppExecutorBridge,
  POD_UNSUPPORTED_CAPABILITIES,
} from "../../src/app-worker/capability-bridge.js";
import type { AppCapabilityBridge } from "../../src/app-worker/capability-bridge.js";
import { AppCronService, MemoryCronStore } from "../../src/app-worker/cron.js";
import { AppWorkerExecutor } from "../../src/app-worker/executor.js";
import type { ResolvedApp } from "../../src/app-worker/bundle.js";
import { appManifest } from "../harness/app-fixtures.js";

const WORKER_TOKEN = "gy_wk_test_token";
const SLUG = "demo";

/** Every capability the fixture manifest grants (so tests reach dispatch). */
const GRANTED = [
  "data.query",
  "vector.upsert", "vector.search", "vector.delete",
  "graph.nodes", "graph.edges", "graph.neighbors", "graph.remove",
  "connector.status", "connector.tools", "connector.call",
  "cron.schedule", "cron.cancel", "cron.list",
  "db.exec", "graph.cypher", "web.fetch", "notify.send", "secrets.get", "fs.read",
  "totally.unknown",
];

interface Seen {
  path: string;
  method: string;
  bearer: string | undefined;
  body: unknown;
}

// ── the mock control plane ──────────────────────────────────────────────────

let server: Server;
let base = "";
let seen: Seen[] = [];

function lastSeen(pathSuffix: string): Seen | undefined {
  return [...seen].reverse().find((s) => s.path.endsWith(pathSuffix));
}

beforeAll(async () => {
  server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      const body: unknown = raw ? JSON.parse(raw) : undefined;
      const path = req.url ?? "";
      const bearer = req.headers.authorization?.replace(/^Bearer /, "");
      seen.push({ path, method: req.method ?? "", bearer, body });

      const json = (status: number, payload: unknown) => {
        res.writeHead(status, { "content-type": "application/json" }).end(JSON.stringify(payload));
      };
      if (bearer !== WORKER_TOKEN) {
        json(401, { error: { code: "E_UNAUTHENTICATED", message: "invalid or revoked worker token" } });
        return;
      }
      const b = (body ?? {}) as Record<string, unknown>;
      // A foreign slug 403s — the worker token's server-side scope rule.
      if (path.startsWith("/api/apps/") && !path.startsWith(`/api/apps/${SLUG}/`)) {
        json(403, { error: { code: "E_FORBIDDEN", message: `this worker token is scoped to '${SLUG}'` } });
        return;
      }
      if (path === `/api/apps/${SLUG}/data/query`) {
        json(200, { data: { rows: [{ echoed: b }], rowCount: 1 } });
      } else if (path === `/api/apps/${SLUG}/data/embeddings`) {
        json(200, { data: { upserted: (b.items as unknown[]).length } });
      } else if (path === `/api/apps/${SLUG}/data/embeddings/search`) {
        json(200, { data: { hits: [{ kind: "notes", ref: "n1", content: "hello world", meta: { tag: "t" }, score: 0.91 }] } });
      } else if (path === `/api/apps/${SLUG}/data/embeddings/remove`) {
        json(200, { data: { removed: (b.refs as unknown[]).length } });
      } else if (path === `/api/apps/${SLUG}/data/graph/nodes`) {
        json(200, { data: { upserted: (b.nodes as unknown[]).length } });
      } else if (path === `/api/apps/${SLUG}/data/graph/edges`) {
        json(200, { data: { upserted: (b.edges as unknown[]).length } });
      } else if (path === `/api/apps/${SLUG}/data/graph/neighbors`) {
        json(200, { data: { nodes: [{ id: "b", kind: "note", props: {}, depth: 1 }], edges: [{ src: "a", dst: "b", kind: "ref" }] } });
      } else if (path === `/api/apps/${SLUG}/data/graph/remove`) {
        json(200, { data: { nodes: 1, edges: 2 } });
      } else if (path === "/api/connections" && req.method === "GET") {
        json(200, {
          data: [
            { slug: "salesforce", name: "Salesforce", kind: "pipedream", scope: "org", enabled: true, hasCredentials: true },
            { slug: "gmail", name: "Gmail", kind: "pipedream", scope: "org", enabled: false, hasCredentials: false },
          ],
        });
      } else if (/^\/api\/connectors\/apps\/[^/]+\/tools$/.test(path)) {
        json(200, { data: { tools: [{ key: "sf-run-query", name: "Run Query", description: "SOQL", inputSchema: { type: "object" } }] } });
      } else if (path === "/api/connections/run") {
        if (b.tool === "forbidden-tool") {
          json(403, { error: { code: "E_FORBIDDEN", message: "tool not in the org grant" } });
          return;
        }
        json(200, { data: { result: { ret: "ran" } } });
      } else {
        json(404, { error: { code: "E_NOT_FOUND", message: `no route ${path}` } });
      }
    });
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const addr = server.address() as { port: number };
  base = `http://127.0.0.1:${addr.port}`;
});

afterAll(async () => {
  await new Promise<void>((r) => server.close(() => r()));
});

beforeEach(() => {
  seen = [];
});

function makeBridge(over: Partial<Parameters<typeof createCapabilityBridge>[0]> = {}): AppCapabilityBridge {
  return createCapabilityBridge({
    controlPlaneUrl: base,
    workerToken: WORKER_TOKEN,
    slug: SLUG,
    manifest: { capabilities: GRANTED, connectors: ["salesforce"] },
    orgId: "org-1",
    ...over,
  });
}

// ── grant + refusal shapes ──────────────────────────────────────────────────

describe("capability bridge — grants and refusals", () => {
  it("answers ping locally (grant-exempt, the slice-2 contract)", async () => {
    const bridge = makeBridge({ manifest: { capabilities: [], connectors: [] } });
    expect(await bridge("ping", { x: 1 })).toEqual({ pong: true, args: { x: 1 } });
    expect(seen).toHaveLength(0); // never left the pod
  });

  it("denies an undeclared capability BEFORE any dispatch (desktop message)", async () => {
    const bridge = makeBridge({ manifest: { capabilities: ["data.query"], connectors: [] } });
    await expect(bridge("vector.upsert", {})).rejects.toThrow(/^capability not granted: vector\.upsert$/);
    expect(seen).toHaveLength(0); // the control plane never saw it
  });

  it("refuses every desktop-only capability with the one message shape", async () => {
    const bridge = makeBridge();
    for (const method of ["web.fetch", "notify.send", "secrets.get", "fs.read"]) {
      await expect(bridge(method, {})).rejects.toThrow(
        new RegExp(`^capability "${method.replace(".", "\\.")}" is not available in the pod runtime yet$`),
      );
    }
    expect(seen).toHaveLength(0);
    // The set covers the whole desktop surface the pod does not serve.
    for (const m of ["llm.complete", "agent.run", "contacts.search", "messages.send", "fs.pick", "fs.write"]) {
      expect(POD_UNSUPPORTED_CAPABILITIES.has(m), m).toBe(true);
    }
  });

  it("points db.exec at the relational data plane (state lives in the planes)", async () => {
    const bridge = makeBridge();
    await expect(bridge("db.exec", { sql: "select 1" })).rejects.toThrow(
      /capability "db\.exec" is not available in the pod runtime yet — app state lives in the data planes only; use data\.query/,
    );
  });

  it("points graph.cypher at the graph plane surfaces", async () => {
    const bridge = makeBridge();
    await expect(bridge("graph.cypher", { query: "MATCH (n) RETURN n" })).rejects.toThrow(
      /capability "graph\.cypher" is not available in the pod runtime yet — the graph plane has no traversal language; use graph\.nodes \/ graph\.edges \/ graph\.neighbors \/ graph\.remove/,
    );
  });

  it("keeps the desktop's unknown-capability message for names off the map", async () => {
    const bridge = makeBridge();
    await expect(bridge("totally.unknown", {})).rejects.toThrow(/^unknown capability: totally\.unknown$/);
  });
});

// ── data-plane dispatch ─────────────────────────────────────────────────────

describe("capability bridge — data planes over the worker token", () => {
  it("data.query passes the structured query through under the gy_wk_ bearer", async () => {
    const bridge = makeBridge();
    const result = await bridge("data.query", { op: "select", table: "notes", limit: 5 });
    expect(result).toEqual({ rows: [{ echoed: { op: "select", table: "notes", limit: 5 } }], rowCount: 1 });
    const req = lastSeen("/data/query");
    expect(req?.bearer).toBe(WORKER_TOKEN);
    expect(req?.body).toEqual({ op: "select", table: "notes", limit: 5 });
  });

  it("vector.upsert maps the desktop shape (collection/id/text/metadata) onto the embeddings plane", async () => {
    const bridge = makeBridge();
    const result = await bridge("vector.upsert", {
      collection: "notes",
      items: [{ id: "n1", text: "hello", metadata: { tag: "t" } }, { id: "n2", text: "world" }],
    });
    expect(result).toEqual({ upserted: 2, kind: "notes", collection: "notes" });
    expect(lastSeen("/data/embeddings")?.body).toEqual({
      items: [
        { ref: "n1", content: "hello", kind: "notes", meta: { tag: "t" } },
        { ref: "n2", content: "world", kind: "notes" },
      ],
    });
  });

  it("vector.upsert also takes plane-native items ({ ref, content, meta })", async () => {
    const bridge = makeBridge();
    await bridge("vector.upsert", { kind: "notes", items: [{ ref: "n1", content: "hello" }] });
    expect(lastSeen("/data/embeddings")?.body).toEqual({ items: [{ ref: "n1", content: "hello", kind: "notes" }] });
  });

  it("vector.upsert refuses vector-only items — the server computes vectors", async () => {
    const bridge = makeBridge();
    await expect(bridge("vector.upsert", { collection: "notes", items: [{ id: "n1", vector: [1, 2] }] })).rejects.toThrow(
      /item "n1" needs content \(or text\)/,
    );
    await expect(bridge("vector.upsert", { collection: "notes", items: [] })).rejects.toThrow(/items is required/);
  });

  it("vector.search maps query/k → text/limit and answers desktop-shaped matches", async () => {
    const bridge = makeBridge();
    const result = await bridge("vector.search", { collection: "notes", query: "hello", k: 3 });
    expect(lastSeen("/embeddings/search")?.body).toEqual({ text: "hello", kind: "notes", limit: 3 });
    expect(result).toEqual({
      matches: [
        {
          id: "n1", ref: "n1",
          score: 0.91,
          text: "hello world", content: "hello world",
          metadata: { tag: "t" }, meta: { tag: "t" },
          kind: "notes",
        },
      ],
    });
  });

  it("vector.delete removes by refs (desktop `id` maps to one ref); whole-kind delete is refused", async () => {
    const bridge = makeBridge();
    expect(await bridge("vector.delete", { collection: "notes", id: "n1" })).toEqual({ deleted: 1, removed: 1 });
    expect(lastSeen("/embeddings/remove")?.body).toEqual({ kind: "notes", refs: ["n1"] });

    expect(await bridge("vector.delete", { kind: "notes", refs: ["a", "b"] })).toEqual({ deleted: 2, removed: 2 });
    await expect(bridge("vector.delete", { collection: "notes" })).rejects.toThrow(/deleting a whole kind is not supported/);
  });

  it("dispatches the graph plane: nodes / edges / neighbors / remove", async () => {
    const bridge = makeBridge();
    expect(await bridge("graph.nodes", { nodes: [{ id: "a" }, { id: "b", kind: "note" }] })).toEqual({ upserted: 2 });
    expect(await bridge("graph.edges", { edges: [{ src: "a", dst: "b", kind: "ref" }] })).toEqual({ upserted: 1 });

    const neighbors = await bridge("graph.neighbors", { id: "a", depth: 2, limit: 10 });
    expect(lastSeen("/graph/neighbors")?.body).toEqual({ id: "a", depth: 2, limit: 10 });
    expect(neighbors).toEqual({ nodes: [{ id: "b", kind: "note", props: {}, depth: 1 }], edges: [{ src: "a", dst: "b", kind: "ref" }] });

    expect(await bridge("graph.remove", { nodes: ["a"], edges: [{ src: "a", dst: "b" }] })).toEqual({ nodes: 1, edges: 2 });
    await expect(bridge("graph.remove", {})).rejects.toThrow(/nodes or edges is required/);
    await expect(bridge("graph.nodes", {})).rejects.toThrow(/nodes is required/);
    await expect(bridge("graph.neighbors", {})).rejects.toThrow(/id is required/);
  });

  it("surfaces a control-plane refusal with the capability and HTTP status attached", async () => {
    // A bridge whose token the mock rejects — every data call 401s.
    const bridge = makeBridge({ workerToken: "gy_wk_revoked" });
    await expect(bridge("data.query", { op: "select", table: "notes" })).rejects.toThrow(
      /data\.query refused by the control plane \(HTTP 401 E_UNAUTHENTICATED\): invalid or revoked worker token/,
    );
  });
});

// ── connectors ──────────────────────────────────────────────────────────────

describe("capability bridge — connectors (org-declared only)", () => {
  it("connector.call runs a declared connector's tool through /api/connections/run", async () => {
    const bridge = makeBridge();
    const result = await bridge("connector.call", { connector: "salesforce", tool: "sf-run-query", args: { q: "SELECT" } });
    expect(result).toEqual({ result: { ret: "ran" } });
    expect(lastSeen("/api/connections/run")?.body).toEqual({ slug: "salesforce", tool: "sf-run-query", props: { q: "SELECT" } });
  });

  it("connector.call refuses an undeclared connector locally (desktop message)", async () => {
    const bridge = makeBridge();
    await expect(bridge("connector.call", { connector: "hubspot", tool: "t" })).rejects.toThrow(
      /connector not declared in manifest: hubspot/,
    );
    expect(seen).toHaveLength(0);
    await expect(bridge("connector.call", { connector: "salesforce" })).rejects.toThrow(/tool is required/);
  });

  it("surfaces the server's 403 (tool outside the org grant) as a clear error", async () => {
    const bridge = makeBridge();
    await expect(bridge("connector.call", { connector: "salesforce", tool: "forbidden-tool" })).rejects.toThrow(
      /connector\.call refused by the control plane \(HTTP 403 E_FORBIDDEN\): tool not in the org grant/,
    );
  });

  it("connector.status reports declared connectors against the resolved connection list", async () => {
    const bridge = makeBridge({ manifest: { capabilities: GRANTED, connectors: ["salesforce", "gmail", "netsuite"] } });
    expect(await bridge("connector.status", {})).toEqual([
      { connector: "salesforce", connected: true, healthy: true }, // enabled
      { connector: "gmail", connected: false, healthy: false }, // listed but disabled
      { connector: "netsuite", connected: false, healthy: false }, // not connected at all
    ]);
  });

  it("connector.status degrades to disconnected when the surface is unreachable (desktop parity)", async () => {
    const bridge = makeBridge({ controlPlaneUrl: "http://127.0.0.1:1", manifest: { capabilities: GRANTED, connectors: ["salesforce"] } });
    expect(await bridge("connector.status", {})).toEqual([{ connector: "salesforce", connected: false, healthy: false }]);
  });

  it("connector.tools lists declared connectors' tools and scopes the `connector` filter", async () => {
    const bridge = makeBridge();
    expect(await bridge("connector.tools", {})).toEqual([
      { connector: "salesforce", key: "sf-run-query", name: "Run Query", description: "SOQL", inputSchema: { type: "object" } },
    ]);
    await expect(bridge("connector.tools", { connector: "hubspot" })).rejects.toThrow(/connector not declared in manifest: hubspot/);
  });
});

// ── cron through the bridge ─────────────────────────────────────────────────

describe("capability bridge — cron", () => {
  it("dispatches cron.schedule/list/cancel to the durable service under the (org, app) scope", async () => {
    const store = new MemoryCronStore();
    const cron = new AppCronService({ executor: { invokeHandler: () => Promise.resolve(null) }, store });
    try {
      const bridge = makeBridge({ cron });
      expect(await bridge("cron.schedule", { id: "daily", cron: "0 9 * * *", handler: "digest" })).toEqual({
        scheduled: true,
        id: "daily",
      });
      expect(await bridge("cron.list", {})).toEqual([{ id: "daily", cron: "0 9 * * *", handler: "digest" }]);
      // The rows landed under the worker token's org, not some global bucket.
      expect(await store.list("org-1", SLUG)).toHaveLength(1);
      expect(await bridge("cron.cancel", { id: "daily" })).toEqual({ cancelled: true });
      expect(await bridge("cron.list", {})).toEqual([]);
    } finally {
      cron.stop();
    }
  });

  it("refuses cron.* precisely when no cron service is wired", async () => {
    const bridge = makeBridge(); // no cron
    await expect(bridge("cron.schedule", { id: "x", cron: "* * * * *", handler: "h" })).rejects.toThrow(
      /capability "cron\.schedule" is not available in the pod runtime yet — this pod has no cron service wired/,
    );
  });
});

// ── end to end: a worker script through the executor to the mock plane ──────

describe("capability bridge — worker script end to end", () => {
  const executors: AppWorkerExecutor[] = [];
  afterEach(async () => {
    await Promise.all(executors.splice(0).map((e) => e.disposeAll()));
  });

  function materializedApp(workerSource: string): () => Promise<ResolvedApp> {
    const dir = mkdtempSync(join(tmpdir(), "app-bridge-"));
    const manifest = appManifest({ slug: SLUG, worker: "worker.js", capabilities: GRANTED, connectors: ["salesforce"] });
    writeFileSync(join(dir, "manifest.json"), JSON.stringify(manifest));
    writeFileSync(join(dir, "index.html"), "<html>");
    writeFileSync(join(dir, "worker.js"), workerSource);
    const app: ResolvedApp = { dir, manifest, sha256: "beef".repeat(16) };
    return () => Promise.resolve(app);
  }

  it("glyphh.call('vector.upsert') travels bridge → worker token → mock control plane", async () => {
    const resolveApp = materializedApp(`
      glyphh.handle("ingest", async (args) => {
        const up = await glyphh.call("vector.upsert", {
          collection: "notes",
          items: [{ id: args.id, text: args.text }],
        });
        const found = await glyphh.call("vector.search", { collection: "notes", query: args.text, k: 1 });
        return { up, top: found.matches[0].id };
      });
    `);
    const exec = new AppWorkerExecutor({ resolveApp, capabilityBridge: singleAppExecutorBridge(SLUG, makeBridge()) });
    executors.push(exec);

    const result = await exec.invokeHandler(SLUG, "ingest", { id: "n1", text: "hello" });
    expect(result).toEqual({ up: { upserted: 1, kind: "notes", collection: "notes" }, top: "n1" });
    expect(lastSeen("/data/embeddings")?.bearer).toBe(WORKER_TOKEN);
    expect(lastSeen("/data/embeddings")?.body).toEqual({ items: [{ ref: "n1", content: "hello", kind: "notes" }] });
  });

  it("a worker's undeclared call is denied inside the sandbox, not at the server", async () => {
    const resolveApp = materializedApp(`
      glyphh.handle("sneak", async () => {
        try {
          await glyphh.call("web.fetch", { url: "https://example.com" });
          return "allowed";
        } catch (err) {
          return String(err.message);
        }
      });
    `);
    // The manifest grants web.fetch (it is in GRANTED) but the pod refuses it;
    // narrow the grant instead to show the denied-before-dispatch path too.
    const bridge = makeBridge({ manifest: { capabilities: ["data.query"], connectors: [] } });
    const exec = new AppWorkerExecutor({ resolveApp, capabilityBridge: executorBridge(new Map([[SLUG, bridge]])) });
    executors.push(exec);

    expect(await exec.invokeHandler(SLUG, "sneak")).toBe("capability not granted: web.fetch");
    expect(seen).toHaveLength(0);
  });

  it("the executor adapter refuses a slug with no bridge (no credential fallthrough)", async () => {
    const resolveApp = materializedApp(`glyphh.handle("go", () => glyphh.call("data.query", {}));`);
    const exec = new AppWorkerExecutor({ resolveApp, capabilityBridge: executorBridge(new Map()) });
    executors.push(exec);
    await expect(exec.invokeHandler(SLUG, "go")).rejects.toThrow(/no capability bridge for app "demo"/);
  });
});
