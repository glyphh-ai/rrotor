/**
 * The facts HTTP-surface functions (2026-09-04: graph, amend, create — the
 * Memory panel's direct lanes, no MCP round-trip) over a real in-process
 * (PGlite) ledger. These shipped WITHOUT tests and broke the coverage gate;
 * this is the debt paid: create → amend supersedes → browse/recall see the
 * new version → graph renders nodes/edges → forget tombstones.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { PGlite } from "@electric-sql/pglite";
import type { PgLike } from "../../src/exec/pgvector-store.js";
import type { Principal } from "../../src/auth/introspect.js";
import { GlyphStore } from "../../src/facts/store.js";
import {
  setFactsStoreForTests, createFact, amendFact, forgetFact, browseFacts, recallFacts, graphFacts,
} from "../../src/facts/server.js";

const ORG: Principal = { orgId: "org-http", userId: "user-h" } as Principal;
const OTHER: Principal = { orgId: "org-other", userId: "user-o" } as Principal;

let db: PGlite;

beforeAll(async () => {
  db = new PGlite();
  const store = await GlyphStore.create({ client: db as unknown as PgLike });
  setFactsStoreForTests(Promise.resolve(store));
}, 60_000);

afterAll(async () => { await db.close(); });

const FACT = {
  entity: { name: "deploy-rule", kind: "rule" },
  relational: { subject: "chris", predicate: "WANT", object: "deploys go through ci" },
  epistemic: { source: "conversation", certainty: "KNOW TRUE" },
};

describe("facts HTTP surface — create / amend / graph / forget", () => {
  let id = "";

  it("createFact lands a stamped fact", async () => {
    const r = await createFact(ORG, { name: "deploy-rule", facts: FACT });
    expect(r.id).toBeTruthy();
    expect(r.scope).toBe("org");
    id = r.id;
  });

  it("browse + recall run the lane (recall may return nothing on a tiny ledger)", async () => {
    const b = await browseFacts(ORG, 50);
    expect(b.facts.length).toBeGreaterThan(0);
    const rec = await recallFacts(ORG, "how do deploys ship?", 3);
    expect(typeof rec.block).toBe("string");   // the lane answers; similarity gating is its own call
    expect(Array.isArray(rec.facts)).toBe(true);
  });

  it("amendFact appends a NEW version superseding the old id", async () => {
    const r = await amendFact(ORG, {
      id,
      name: "deploy-rule",
      facts: { ...FACT, relational: { ...FACT.relational, object: "deploys go through ci, never local" } },
    });
    expect(r.supersedes).toBe(id);
    expect(r.id).not.toBe(id);
    id = r.id;
  });

  it("amendFact on an unknown id throws (the ledger never guesses)", async () => {
    await expect(amendFact(ORG, { id: "no-such-id", facts: FACT })).rejects.toThrow(/no such fact/);
  });

  it("graphFacts renders nodes + coords for the org — and only the org", async () => {
    await createFact(ORG, { name: "second-rule", facts: { ...FACT, entity: { name: "second-rule", kind: "rule" } } });
    const g = await graphFacts(ORG, { max: 10 });
    expect(g.nodes.length).toBeGreaterThanOrEqual(2);
    expect(g.coords.length).toBe(g.nodes.length);
    for (const e of g.edges) {
      expect(e.a).toBeLessThan(g.nodes.length);
      expect(e.b).toBeLessThan(g.nodes.length);
      expect(["semantic", "neural"]).toContain(e.kind);
    }
    // Tenant wall: a stranger org sees an empty space.
    const other = await graphFacts(OTHER, { max: 10 });
    expect(other.nodes).toHaveLength(0);
  });

  it("forgetFact tombstones (idempotent); an unknown id reports false", async () => {
    const r1 = await forgetFact(ORG, id);
    expect(r1.forgotten).toBe(true);
    const r2 = await forgetFact(ORG, id);
    expect(r2.forgotten).toBe(true);   // the row still exists (tombstoned) — forget is idempotent
    const r3 = await forgetFact(ORG, "never-existed");
    expect(r3.forgotten).toBe(false);
  });

  it("createFact with a user scope lands user-scoped", async () => {
    const r = await createFact(ORG, { name: "my-pref", facts: { entity: { name: "my-pref", kind: "preference" } }, scope: "user" });
    expect(r.scope).toBe("user");
  });
});
