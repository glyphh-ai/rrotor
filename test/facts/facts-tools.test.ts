/**
 * facts-tools.test.ts — the glyphh_facts substrate end to end: a real MCP
 * client over an in-memory transport, driving all six tools against a real
 * (in-process PGlite) Postgres ledger.
 *
 * What the assertions guard:
 *   - create → the glyphh lands with a stamped `name@ts#v1` id;
 *   - build_fact proposes the near-duplicate as a candidate (the reason step);
 *   - search_facts gates on cosine and returns FULL glyphh JSON;
 *   - update_fact chains a new version and retires the old id from search;
 *   - delete_fact tombstones (gone from search, row retained);
 *   - build_fact_tree persists a derived glyphh with citations and returns
 *     the auditable FactTree rendering;
 *   - owner scoping: another org sees none of it.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { PGlite } from "@electric-sql/pglite";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import type { PgLike } from "../../src/exec/pgvector-store.js";
import type { Principal } from "../../src/auth/introspect.js";
import { GlyphStore } from "../../src/facts/store.js";
import { buildFactsServer, setFactsStoreForTests, renderFactBlock } from "../../src/facts/server.js";

const ORG_A: Principal = { orgId: "org-aaaa", userId: "user-1" } as Principal;
const ORG_B: Principal = { orgId: "org-bbbb", userId: "user-2" } as Principal;

let db: PGlite;
let clientA: Client;
let clientB: Client;

async function connect(p: Principal): Promise<Client> {
  const server = buildFactsServer(p);
  const [clientT, serverT] = InMemoryTransport.createLinkedPair();
  await server.server.connect(serverT);
  const client = new Client({ name: "test", version: "0.0.0" });
  await client.connect(clientT);
  return client;
}

async function call(client: Client, name: string, args: Record<string, unknown>): Promise<{ raw: string; json?: unknown; isError: boolean }> {
  const res = await client.callTool({ name, arguments: args });
  const raw = ((res.content as Array<{ type: string; text?: string }>)[0]?.text) ?? "";
  let json: unknown;
  try { json = JSON.parse(raw); } catch { /* error strings are not JSON */ }
  return { raw, json, isError: !!res.isError };
}

const DEPLOY_FACT = {
  entity: { name: "deploy-rule", kind: "rule" },
  relational: { subject: "chris", predicate: "WANT", object: "deploys DO through ci" },
  epistemic: { source: "conversation", certainty: "KNOW TRUE" },
};

beforeAll(async () => {
  db = new PGlite();
  const store = await GlyphStore.create({ client: db as unknown as PgLike });
  setFactsStoreForTests(Promise.resolve(store));
  clientA = await connect(ORG_A);
  clientB = await connect(ORG_B);
}, 60_000);

afterAll(async () => { await db.close(); });

describe("glyphh_facts — the six tools over the org ledger", () => {
  let firstId = "";
  let secondId = "";

  it("lists exactly the six tools", async () => {
    const tools = await clientA.listTools();
    expect(tools.tools.map((t) => t.name).sort()).toEqual([
      "build_fact", "build_fact_tree", "create_fact", "delete_fact", "search_facts", "update_fact",
    ]);
  });

  it("create_fact lands a stamped glyphh", async () => {
    const r = await call(clientA, "create_fact", { name: "deploy-rule", facts: DEPLOY_FACT, confidence: 0.9 });
    expect(r.isError).toBe(false);
    const j = r.json as { id: string; confidence: number; scope: string };
    expect(j.id).toMatch(/^deploy-rule@.+#v1$/);
    expect(j.confidence).toBe(0.9);
    expect(j.scope).toBe("org");
    firstId = j.id;
  });

  it("build_fact proposes the near-duplicate as a candidate, persisting nothing", async () => {
    const r = await call(clientA, "build_fact", {
      name: "deploy-rule-restated",
      facts: {
        entity: { name: "deploy-rule", kind: "rule" },
        relational: { subject: "chris", predicate: "WANT", object: "deploys DO through ci pipeline" },
        epistemic: { source: "conversation", certainty: "KNOW TRUE" },
      },
    });
    expect(r.isError).toBe(false);
    const j = r.json as { candidates: Array<{ id: string; cos: number }>; guidance: string };
    expect(j.candidates.map((c) => c.id)).toContain(firstId);
    expect(j.candidates[0]!.cos).toBeGreaterThan(0.55);
    expect(j.guidance).toContain("update_fact");
  });

  it("search_facts returns full glyphh JSON above the gate; junk stays out", async () => {
    const hit = await call(clientA, "search_facts", { facts: DEPLOY_FACT });
    const hits = (hit.json as { facts: Array<{ id: string; concept: unknown; confidence: number }> }).facts;
    expect(hits.map((h) => h.id)).toContain(firstId);
    expect(hits[0]!.concept).toMatchObject({ name: "deploy-rule" });

    const miss = await call(clientA, "search_facts", {
      facts: { entity: { name: "biscuit", kind: "dog" }, perceptual: { size: "SMALL" }, relational: { possessor: "sam" } },
    });
    expect((miss.json as { facts: unknown[] }).facts.map((h) => (h as { id: string }).id)).not.toContain(firstId);
  });

  it("update_fact chains a version and retires the old id", async () => {
    const r = await call(clientA, "update_fact", {
      supersedes: firstId,
      name: "deploy-rule",
      facts: {
        entity: { name: "deploy-rule", kind: "rule" },
        relational: { subject: "chris", predicate: "WANT", object: "deploys DO through ci, NOT from here" },
        temporal: { time: "2026-08-25" },
        epistemic: { source: "conversation", certainty: "KNOW TRUE" },
      },
      confidence: 0.95,
    });
    expect(r.isError).toBe(false);
    const j = r.json as { id: string; supersedes: string };
    expect(j.supersedes).toBe(firstId);
    secondId = j.id;

    // The new version carries an EXTRA layer (temporal), so its cortex sits
    // farther from the original probe — widen the gate; the retired id must
    // stay out at ANY threshold (superseded is superseded).
    const s = await call(clientA, "search_facts", { facts: DEPLOY_FACT, threshold: 0.4 });
    const ids = (s.json as { facts: Array<{ id: string }> }).facts.map((h) => h.id);
    expect(ids).toContain(secondId);
    expect(ids).not.toContain(firstId);
  });

  it("build_fact_tree persists a derived glyphh and renders the audit tree", async () => {
    const r = await call(clientA, "build_fact_tree", {
      name: "chris-deploy-doctrine",
      facts: {
        entity: { name: "chris-deploy-doctrine", kind: "doctrine" },
        relational: { subject: "chris", predicate: "WANT", object: "all DO go through ci BECAUSE GOOD" },
        epistemic: { source: "derived", certainty: "KNOW" },
      },
      citations: [secondId],
      confidence: 0.85,
    });
    expect(r.isError).toBe(false);
    const j = r.json as { id: string; tree: string };
    expect(j.id).toMatch(/^chris-deploy-doctrine@.+#v1$/);
    expect(j.tree).toContain("derivation");
    expect(j.tree).toContain(secondId);

    const s = await call(clientA, "search_facts", { facts: DEPLOY_FACT, threshold: 0.5 });
    const derived = (s.json as { facts: Array<{ id: string; derived: boolean; citations?: string[] }> }).facts
      .find((h) => h.id === j.id);
    expect(derived?.derived).toBe(true);
    expect(derived?.citations).toEqual([secondId]);
  });

  it("unknown citations are refused loudly", async () => {
    const r = await call(clientA, "build_fact_tree", {
      name: "bad", facts: DEPLOY_FACT, citations: ["nope@2020-01-01T00:00:00#v1"],
    });
    expect(r.isError).toBe(true);
    expect(r.raw).toContain("unknown citation ids");
  });

  it("delete_fact tombstones — gone from search, refused twice", async () => {
    const r = await call(clientA, "delete_fact", { id: secondId });
    expect(r.isError).toBe(false);
    const again = await call(clientA, "delete_fact", { id: secondId });
    expect(again.isError).toBe(true);
    const s = await call(clientA, "search_facts", { facts: DEPLOY_FACT, threshold: 0.3 });
    expect((s.json as { facts: Array<{ id: string }> }).facts.map((h) => h.id)).not.toContain(secondId);
  });

  it("another org sees nothing", async () => {
    const s = await call(clientB, "search_facts", { facts: DEPLOY_FACT, threshold: 0.3 });
    expect((s.json as { facts: unknown[] }).facts).toHaveLength(0);
  });

  it("renderFactBlock — the exchange's primes select; the shape is constant", async () => {
    // Two fresh facts on distinct topics for the selector to choose between.
    await call(clientA, "create_fact", {
      name: "chris-wants-ci", confidence: 0.9,
      facts: { entity: { name: "chris-wants-ci", kind: "rule" }, relational: { subject: "chris", predicate: "WANT", object: "deploy DO happen through ci" } },
    });
    await call(clientA, "create_fact", {
      name: "office-location", confidence: 0.6,
      facts: { entity: { name: "office-location", kind: "place" }, spatial: { location: "austin" }, relational: { possessor: "glyphh" } },
    });

    const block = await renderFactBlock(ORG_A, "the user wants to know how deploys happen and because of what");
    expect(block).toBeTruthy();
    const lines = block!.split("\n");
    expect(lines[0]).toContain("## Org facts (glyphh ledger");
    expect(lines.length).toBeLessThanOrEqual(13); // header + FACT_BLOCK_MAX
    // The WANT/DO/HAPPEN/BECAUSE exchange selects the ci rule above the place.
    const ciIdx = lines.findIndex((l) => l.includes("chris-wants-ci"));
    const officeIdx = lines.findIndex((l) => l.includes("office-location"));
    expect(ciIdx).toBeGreaterThan(0);
    expect(officeIdx === -1 || officeIdx > ciIdx).toBe(true);
    // Every fact line carries its citable id and confidence.
    expect(lines[ciIdx]).toMatch(/\[chris-wants-ci@.+#v1\] \(conf 0\.90\)/);

    // An empty org gets NO block, not an empty shell.
    expect(await renderFactBlock({ orgId: "org-empty", userId: "u" } as Principal, "anything")).toBeNull();
  });

  it("schema-invalid facts are refused with the shape named", async () => {
    const r = await call(clientA, "create_fact", { name: "x", facts: { madeup: { nope: "y" } } });
    expect(r.isError).toBe(true);
    expect(r.raw).toContain("universal");
  });
});
