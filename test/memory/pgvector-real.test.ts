/**
 * E5 — the pgvector stator against a REAL Postgres + pgvector, not the in-process
 * PGlite. Exercises the actual `pg` driver, a real `hnsw` index, and durable
 * hydrate-across-restart on a live server. Skipped unless ROTOR_TEST_PG_URL points
 * at a Postgres with the `vector` extension (see scripts/pg-setup.sh); CI provisions
 * one, so this runs green there and is a no-op on a dev box without a server.
 */

import { describe, it, expect, beforeAll } from "vitest";

import { PgVectorStore } from "../../src/exec/pgvector-store.js";
import { buildBasicPlugins } from "../../src/plugins/index.js";
import type { Stator } from "../../src/exec/store.js";

const URL = process.env.ROTOR_TEST_PG_URL;
const fillers = (fs: { filler: string }[]) => fs.map((f) => f.filler);
const mem = (store: Stator) => buildBasicPlugins({ store }).memory;

// Drop the store's tables so each run starts clean on the shared database.
async function reset(): Promise<void> {
  const mod: any = await import("pg");
  const Pool = mod.Pool ?? mod.default.Pool;
  const pool = new Pool({ connectionString: URL });
  await pool.query("DROP TABLE IF EXISTS step_records, result_cache, facts, sessions, kv, turns CASCADE;");
  await pool.end();
}

describe.skipIf(!URL)("pgvector stator — real Postgres", () => {
  beforeAll(async () => {
    await reset();
  });

  it("scopes tiers/sessions and supersedes facts identically on a live server", async () => {
    const store = await PgVectorStore.create({ url: URL });
    const m = mem(store);
    store.touchSession("s1");
    store.writeFacts([{ entity: "user", role: "note", filler: "ephemeral", key: "user:note", is_current: true, tick: 0, tier: "short", session: "s1" }]);
    store.writeFacts([{ entity: "user", role: "pref", filler: "tabs", key: "user:pref", is_current: true, tick: 0, tier: "long", session: "s1" }]);
    // supersession
    store.writeFacts([{ entity: "ada", role: "city", filler: "London", key: "ada:city", is_current: true, tick: 0 }]);
    store.writeFacts([{ entity: "ada", role: "city", filler: "Paris", key: "ada:city", is_current: true, tick: 1 }]);
    expect(store.lookupFact("ada", "city")?.filler).toBe("Paris");

    store.touchSession("s2");
    const s2 = fillers(m.recall({ session: "s2" }));
    expect(s2).toContain("tabs"); // long crosses
    expect(s2).not.toContain("ephemeral"); // short stays in s1
    await store.shutdown();
  });

  it("ranks turns by cosine similarity via a real hnsw index", async () => {
    await reset();
    const store = await PgVectorStore.create({ url: URL });
    store.addTurn("the config parser parseConfig reads yaml");
    store.addTurn("the payment gateway charges a card");
    store.addTurn("kubernetes schedules pods onto nodes");
    await store.flush();
    const hits = await store.semanticRecallDb("config parser parseConfig yaml", 3);
    expect(hits[0].text).toMatch(/parseConfig/);
    await store.shutdown();
  });

  it("hydrates facts + the run tape across a restart (a fresh pod, same DB)", async () => {
    await reset();
    const s1 = await PgVectorStore.create({ url: URL });
    s1.touchSession("sess-A");
    s1.writeFacts([{ entity: "user", role: "pref", filler: "vim", key: "user:pref", is_current: true, tick: 0, tier: "long" }]);
    s1.addTurn("a durable turn");
    await s1.shutdown();

    const s2 = await PgVectorStore.create({ url: URL });
    expect(s2.lookupFact("user", "pref")?.filler).toBe("vim"); // hydrated
    expect(s2.turns()).toContain("a durable turn");
    expect(s2.sessionOrdinal("sess-A")).toBe(0); // sessions hydrated
    await s2.shutdown();
  });
});
