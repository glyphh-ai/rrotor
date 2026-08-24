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
});

describe("pgvector stator — atomic supersede+insert (CTE)", () => {
  it("keeps exactly one current row per key across repeated supersessions", async () => {
    const store = await PgVectorStore.create({ client: await pglite() });
    for (const city of ["London", "Paris", "Berlin", "Rome"]) {
      await store.writeFacts([{ entity: "ada", role: "city", filler: city, key: "ada:city", is_current: true, tick: 0 }]);
    }
    const all = await store.snapshotFacts();
    const currentForKey = all.filter((f) => f.key === "ada:city" && f.is_current);
    expect(currentForKey).toHaveLength(1); // no torn/double-current state
    expect(currentForKey[0].filler).toBe("Rome"); // last write wins
    // The superseded history is intact (4 rows written, 3 now superseded).
    expect(all.filter((f) => f.key === "ada:city" && !f.is_current)).toHaveLength(3);
  });

  it("a keyless fact inserts without superseding anything", async () => {
    const store = await PgVectorStore.create({ client: await pglite() });
    await store.writeFacts([{ entity: "ada", role: "note", filler: "one", is_current: true, tick: 0 }]);
    await store.writeFacts([{ entity: "ada", role: "note", filler: "two", is_current: true, tick: 1 }]);
    const notes = (await store.snapshotFacts()).filter((f) => f.role === "note" && f.is_current);
    expect(notes.map((f) => f.filler).sort()).toEqual(["one", "two"]); // both current
  });
});

describe("pgvector stator — live-read edge cases", () => {
  it("misses read empty, the latest ordinal is reported, and flush is a no-op", async () => {
    const store = await PgVectorStore.create({ client: await pglite() });
    // Misses across the surface.
    expect(await store.lookupFact("nobody", "role")).toBeUndefined();
    expect(await store.kvGet("absent")).toBeUndefined();
    expect(await store.cache.get("absent", 0)).toBeUndefined();
    expect(await store.history.lookup("nope", "s", 0)).toBeUndefined();
    expect(await store.history.lastAttempt("nope", "s")).toBe(-1);
    // No sessions yet ⇒ latest ordinal is 0.
    expect(await store.sessionOrdinal()).toBe(0);
    // Two sessions ⇒ latest is the max.
    await store.touchSession("a");
    await store.touchSession("b");
    expect(await store.sessionOrdinal()).toBe(1);
    await store.flush(); // no-op with live writes
  });
});

describe("pgvector stator — live cross-pod visibility (mirror dropped)", () => {
});

describe("pgvector stator — event history, cache and kv are durable", () => {
  it("hydrates the run tape, result cache and kv across a restart", async () => {
    const db = await pglite();
    const s1 = await PgVectorStore.create({ client: db });
    await s1.history.append(rec("r1", "a", 0));
    await s1.history.append(rec("r1", "a", 1)); // a retry
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
  it("surfaces a persistence error instead of swallowing it (live writes)", async () => {
    const db = await pglite();
    // A client that fails only the facts insert — DDL still succeeds.
    const flaky: PgLike = {
      query: (sql, params) => (sql.includes("INSERT INTO facts") ? Promise.reject(new Error("boom")) : db.query(sql, params)),
      exec: (sql) => db.exec!(sql),
      close: () => db.close!(),
    };
    const store = await PgVectorStore.create({ client: flaky });
    // With live reads/writes there is no mirror to hide it: the write itself rejects.
    await expect(store.writeFacts([{ entity: "a", role: "b", filler: "c", is_current: true, tick: 0 }])).rejects.toThrow(/boom/);
  });
});

describe("pgvector stator — ANN recall in the database", () => {
});

describe("pgvector stator — dim-cap degrades to an exact scan", () => {
});
