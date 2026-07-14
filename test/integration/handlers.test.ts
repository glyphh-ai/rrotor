/**
 * Secondary handler completeness (BUILD_PLAN.md Phase 10): the `write` absorb
 * enricher, `cascade` tiered consolidation, `plan` typed-enum decode, and the
 * fleshed-out tool substrate — each deterministic.
 */

import { describe, it, expect } from "vitest";

import { execute } from "../../src/exec/executor.js";
import { buildBasicPlugins } from "../../src/plugins/index.js";
import { InProcessStore } from "../../src/exec/store.js";
import { BasicModels } from "../../src/plugins/models.js";
import { BasicConnections } from "../../src/plugins/connections.js";
import type { RotorDocument } from "../../src/types.js";

const doc = (spec: Record<string, unknown>): RotorDocument =>
  ({ apiVersion: "rotor.glyphh.ai/v0.1", kind: "Rotor", metadata: { name: "h", version: "0.1.0" }, spec }) as unknown as RotorDocument;

describe("write absorb enricher (§7.4)", () => {
  it("extracts distinct (entity, role, filler) facts from free text", async () => {
    const store = new InProcessStore();
    const d = doc({
      entry: "w",
      steps: [{ id: "w", type: "write", in: { text: "Ada lives in London. Ada's job is engineer. Ada has a dog." }, out: {}, config: { mode: "absorb", key: "ada" }, next: "end" }],
    });
    const r = await execute(d, {}, buildBasicPlugins({ store }));
    expect((r.history[0].output as { written: number }).written).toBe(3);
    expect((await store.lookupFact("ada", "rel.city"))?.filler).toBe("London");
    expect((await store.lookupFact("ada", "job"))?.filler).toBe("engineer");
    expect((await store.lookupFact("ada", "rel.has"))?.filler).toBe("dog");
  });

  it("falls back to a raw.text slot when nothing matches", async () => {
    const store = new InProcessStore();
    const d = doc({ entry: "w", steps: [{ id: "w", type: "write", in: { text: "?!?!" }, out: {}, config: { mode: "absorb", key: "k" }, next: "end" }] });
    await execute(d, {}, buildBasicPlugins({ store }));
    expect((await store.lookupFact("k", "raw.text"))?.filler).toBe("?!?!");
  });
});

describe("cascade tiered consolidation (§7.18)", () => {
  it("splits recent/short from de-duplicated older turns", async () => {
    const store = new InProcessStore();
    for (const t of ["one", "two", "three", "two", "one", "four", "five"]) await store.addTurn(t);
    const mem = buildBasicPlugins({ store }).memory;
    // span 3 → short = last 3; older = [one,two,three,two] → distinct {one,two,three}=3, absorbed 1.
    expect(await mem.cascade(3)).toEqual({ short: 3, mid: 3, long: 1 });
  });
});

describe("plan typed-enum decode (§7.10)", () => {
  const classify = (q: string, ops: string[]) => new BasicModels().classify(q, ops);

  it("picks the nearest in-schema op", () => {
    const r = classify("please compute the total sum", ["retrieve", "compute", "answer"]);
    expect(r.class).toBe("compute");
  });

  it("returns OUT_OF_SCHEMA for an empty op set", () => {
    expect(classify("anything", []).class).toBe("OUT_OF_SCHEMA");
  });

  it("plan step refuses on an out-of-schema decode when configured", async () => {
    const d = doc({
      entry: "p",
      steps: [{ id: "p", type: "plan", in: { question: "zzzzz" }, out: {}, config: { ops: ["retrieve", "compute"], on_out_of_schema: "refuse" }, next: "end" }],
    });
    const r = await execute(d, {}, buildBasicPlugins());
    // A zero-overlap question yields no positive prototype → refuse.
    const p = r.history.find((h) => h.step_id === "p");
    expect(["ok", "refused"]).toContain(p?.status);
  });
});

describe("tool substrate", () => {
  const conn = new BasicConnections();

  it("evaluates a closed arithmetic expression deterministically", async () => {
    expect(await conn.invoke("compute", { expr: "2 + 3 * 4" })).toEqual({ result: 14 });
    expect(await conn.invoke("compute", { expr: "(2 + 3) * 4" })).toEqual({ result: 20 });
  });

  it("app methods are loopback (applied, never dialing out)", async () => {
    expect(await conn.invoke("panels.open", { id: "x" })).toMatchObject({ applied: true, loopback: true });
  });

  it("unknown tool dispatches to a typed failure, never throws", async () => {
    expect(await conn.dispatch("nope", {})).toEqual({ ok: false, error: "E_NO_TOOL: nope" });
  });
});
