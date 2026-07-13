/**
 * The Postgres + pgvector stator (docs/vector-stores.md), exercised against an
 * in-process PGlite instance with the pgvector extension — so the backend is
 * really run in CI, no server required. Covers: tier/session parity with the
 * other backends, durable hydrate-after-restart, ANN recall in the database, and
 * the dim-cap degrade-to-exact-scan path.
 */

import { describe, it, expect, afterEach } from "vitest";
import { PGlite } from "@electric-sql/pglite";
import { vector } from "@electric-sql/pglite/vector";

import { PgVectorStore } from "../../src/exec/pgvector-store.js";
import { buildBasicPlugins } from "../../src/plugins/index.js";
import type { PgLike } from "../../src/exec/pgvector-store.js";
import type { StepRecord } from "../../src/types.js";

const rec = (run: string, step: string, attempt: number): StepRecord =>
  ({ run_id: run, step_id: step, attempt, logical_tick: attempt, status: "ok" }) as unknown as StepRecord;

const open: PGlite[] = [];
async function pglite(): Promise<PgLike> {
  const db = await PGlite.create({ extensions: { vector } });
  open.push(db);
  return db as unknown as PgLike;
}
afterEach(async () => {
  while (open.length) await open.pop()!.close();
});

const fillers = (facts: { filler: string }[]) => facts.map((f) => f.filler);

describe("pgvector stator — tier/session parity", () => {
  it("scopes short/mid/long exactly like the other backends", async () => {
    const store = await PgVectorStore.create({ client: await pglite() });
    const m = buildBasicPlugins({ store }).memory;
    await store.touchSession("s1");
    await m.write([{ entity: "user", role: "note", filler: "ephemeral" }], { session: "s1", tier: "short" });
    await m.write([{ entity: "user", role: "topic", filler: "auth flow" }], { session: "s1", tier: "mid" });
    await m.write([{ entity: "user", role: "pref", filler: "tabs" }], { session: "s1", tier: "long" });

    await store.touchSession("s2");
    const s2 = fillers(await m.recall({ session: "s2", midWindow: 5 }));
    expect(s2).toContain("tabs"); // long
    expect(s2).toContain("auth flow"); // mid, within window
    expect(s2).not.toContain("ephemeral"); // short — belongs to s1
    await store.flush();
  });

  it("runs the same closed ops as the pure engine (supersession-correct)", async () => {
    const store = await PgVectorStore.create({ client: await pglite() });
    await store.writeFacts([{ entity: "ada", role: "city", filler: "London", key: "ada:city", is_current: true, tick: 0 }]);
    await store.writeFacts([{ entity: "ada", role: "city", filler: "Paris", key: "ada:city", is_current: true, tick: 1 }]);
    // Superseded: current is Paris, prev is London.
    expect((await store.lookupFact("ada", "city"))?.filler).toBe("Paris");
    expect((await store.query("prev", { person: "ada", slot: "city" })).rows[0]?.filler).toBe("London");
    await store.flush();
  });
});

describe("pgvector stator — durable hydrate across a restart", () => {
  it("a fresh store over the same database rebuilds facts, turns and sessions", async () => {
    const db = await pglite();
    const s1 = await PgVectorStore.create({ client: db });
    await s1.touchSession("sess-A");
    await s1.writeFacts([{ entity: "user", role: "pref", filler: "tabs", key: "user:pref", is_current: true, tick: 0, tier: "long" }]);
    await s1.addTurn("we decided to use pgvector for the durable stator");
    await s1.flush(); // persist before the "restart"

    // A new pod: construct a second store over the SAME database and hydrate.
    const s2 = await PgVectorStore.create({ client: db });
    expect((await s2.lookupFact("user", "pref"))?.filler).toBe("tabs");
    expect(await s2.turns()).toContain("we decided to use pgvector for the durable stator");
    expect(await s2.sessionOrdinal("sess-A")).toBe(0);
    // A new session continues the ordinal sequence, not restart from 0.
    expect(await s2.touchSession("sess-B")).toBe(1);
    await s2.flush();
  });
});

describe("pgvector stator — event history, cache and kv are durable", () => {
  it("hydrates the run tape, result cache and kv across a restart", async () => {
    const db = await pglite();
    const s1 = await PgVectorStore.create({ client: db });
    await s1.history.append(rec("r1", "a", 0));
    s1.history.append(rec("r1", "a", 1)); // a retry
    expect(await s1.history.lastAttempt("r1", "a")).toBe(1);
    expect(await s1.history.lookup("r1", "a", 0)).toBeDefined();
    await s1.cache.put("k1", { answer: 42 }, { scope: "rotor" });
    await s1.cache.put("k2", { v: 1 }, { scope: "rotor", ttlTicks: 5 });
    expect(await s1.cache.get("k1", 0)).toEqual({ answer: 42 });
    expect(await s1.cache.get("k2", 9)).toBeUndefined(); // expired past its tick
    await s1.kvSet("kk", { hello: "world" });
    expect(await s1.kvGet("kk")).toEqual({ hello: "world" });
    await s1.flush();

    const s2 = await PgVectorStore.create({ client: db });
    expect((await s2.history.read("r1")).length).toBe(2);
    expect(await s2.history.lastAttempt("r1", "a")).toBe(1);
    expect(await s2.cache.get("k1", 0)).toEqual({ answer: 42 });
    expect(await s2.kvGet("kk")).toEqual({ hello: "world" });
    await s2.flush();
  });
});

describe("pgvector stator — lifecycle", () => {
  it("shutdown flushes then releases the client", async () => {
    const db = await PGlite.create({ extensions: { vector } }); // untracked: shutdown closes it
    const store = await PgVectorStore.create({ client: db as unknown as PgLike });
    await store.addTurn("persisted before shutdown");
    await store.shutdown();
    await expect(db.query("SELECT 1")).rejects.toBeDefined(); // client is closed
  });

  it("flush surfaces a persistence error instead of swallowing it", async () => {
    const db = await pglite();
    // A client that fails only the facts insert — DDL + hydrate still succeed.
    const flaky: PgLike = {
      query: (sql, params) => (sql.includes("INSERT INTO facts") ? Promise.reject(new Error("boom")) : db.query(sql, params)),
      exec: (sql) => db.exec!(sql),
      close: () => db.close!(),
    };
    const store = await PgVectorStore.create({ client: flaky });
    await store.writeFacts([{ entity: "a", role: "b", filler: "c", is_current: true, tick: 0 }]);
    // Mirror still has it (control path unaffected); the durability error surfaces on flush.
    expect((await store.lookupFact("a", "b"))?.filler).toBe("c");
    await expect(store.flush()).rejects.toThrow(/boom/);
  });
});

describe("pgvector stator — ANN recall in the database", () => {
  it("ranks turns by cosine similarity via the vector index", async () => {
    const store = await PgVectorStore.create({ client: await pglite() });
    expect(store.vectorIndexed).toBe(true); // 256-dim ⇒ hnsw-indexed
    await store.addTurn("the config parser reads parseConfig from the yaml file");
    await store.addTurn("the pool warms hot instances up to the budget");
    await store.addTurn("the drain forwards step records to an external sink");
    await store.flush();

    const hits = await store.semanticRecallDb("config parser parseConfig yaml", 3);
    expect(hits[0].text).toMatch(/parseConfig/);
    expect(hits[0].score).toBeGreaterThan(hits[hits.length - 1].score - 1e-9);
    await store.flush();
  });
});

describe("pgvector stator — dim-cap degrades to an exact scan", () => {
  it("an embedding dim above the index cap is unindexed but still recalls", async () => {
    const store = await PgVectorStore.create({ client: await pglite(), embedDim: 3000 });
    expect(store.vectorIndexed).toBe(false); // > 2000 ⇒ no hnsw index
    await store.addTurn("alpha beta gamma delta");
    await store.addTurn("completely unrelated words here");
    await store.flush();
    const hits = await store.semanticRecallDb("alpha beta gamma", 2);
    expect(hits[0].text).toBe("alpha beta gamma delta");
    await store.flush();
  });
});
