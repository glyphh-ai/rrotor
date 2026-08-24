/**
 * SQLite stator integration (BUILD_PLAN.md Phase 2): store-level parity with the
 * in-process backend, and restart durability — a run recorded to a file replays
 * identically from a freshly-reopened store, which the in-process backend can
 * never do.
 */

import { describe, it, expect, afterAll } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { InProcessStore, type Stator } from "../../src/exec/store.js";
import { SqliteStore } from "../../src/exec/sqlite-store.js";
import { execute } from "../../src/exec/executor.js";
import { buildBasicPlugins } from "../../src/plugins/index.js";
import { shapeOf } from "../harness/replay.js";
import { loadFixture, defaultInputs } from "../harness/fixtures.js";

const tmp = mkdtempSync(join(tmpdir(), "rotor-stator-"));
afterAll(() => rmSync(tmp, { recursive: true, force: true }));

/** Exercise the full Stator surface and snapshot the observable results, so two
 *  backends can be compared for byte-identical behavior. */
async function exerciseStore(s: Stator): Promise<unknown> {
  await s.writeFacts([
    { entity: "ada", role: "rel.spouse", filler: "morgan", key: "ada:spouse", is_current: true, tick: 1 },
    { entity: "ada", role: "rel.city", filler: "london", is_current: true, tick: 2 },
    { entity: "bob", role: "rel.city", filler: "london", is_current: true, tick: 3 },
  ]);
  // Supersede ada's spouse — prev must see the old value.
  await s.writeFacts([{ entity: "ada", role: "rel.spouse", filler: "sam", key: "ada:spouse", is_current: true, tick: 4 }]);

  await s.cache.put("ck", { v: 42 }, { ttlTicks: 5, scope: "rotor" });
  await s.kvSet("greeting", { hi: true });

  return {
    lookup_spouse: (await s.lookupFact("ada", "rel.spouse"))?.filler,
    fillers_city: await s.fillers("bob", "rel.city"),
    q_lookup: await s.query("lookup", { person: "ada" }),
    q_prev: await s.query("prev", { person: "ada", slot: "rel.spouse" }),
    q_count: await s.query("count", { slot: "rel.city" }),
    q_top: await s.query("top", { slot: "rel.city", k: 5 }),
    q_who: await s.query("who", { slot: "rel.city", value: "london" }),
    q_compare: await s.query("compare", { a: "ada", b: "bob", slot: "rel.city" }),
    cache_live: await s.cache.get("ck", 4),
    cache_expired: await s.cache.get("ck", 5),
    kv: await s.kvGet("greeting"),
  };
}

describe("SQLite store — parity with in-process", () => {
  it("produces identical results across the full Stator surface", async () => {
    const mem = await exerciseStore(new InProcessStore());
    const sqlite = new SqliteStore(":memory:");
    const sql = await exerciseStore(sqlite);
    await sqlite.close();
    expect(sql).toEqual(mem);
  });
});

describe("SQLite store — restart durability", () => {
  it("replays a recorded run from a reopened file, byte-identical", async () => {
    const file = join(tmp, "durable.db");
    const doc = loadFixture("rotors/base.rotor.yaml");
    const inputs = defaultInputs(doc);

    const s1 = new SqliteStore(file);
    const first = await execute(doc, inputs, buildBasicPlugins({ store: s1 }));
    const shape1 = shapeOf(first);
    await s1.close();

    // Reopen the SAME file — history must have survived the close.
    const s2 = new SqliteStore(file);
    const before = (await s2.history.read(first.run_id)).length;
    const second = await execute(doc, inputs, buildBasicPlugins({ store: s2 }));
    const after = (await s2.history.read(first.run_id)).length;
    await s2.close();

    expect(before).toBeGreaterThan(0); // durable across reopen
    expect(after).toBe(before); // replay appended nothing
    expect(shapeOf(second)).toEqual(shape1); // identical replay
  });
});
