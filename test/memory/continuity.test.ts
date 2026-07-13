/**
 * Cross-definition memory continuity: within ONE runtime instance (= one stator),
 * facts written by one rotor definition are recalled by a DIFFERENT definition on a
 * later run. Memory belongs to the runtime/stator, not to a run or a definition —
 * a rotor is just a path through the stator (the "base then super-power" model).
 *
 * Run event history stays per-run (keyed by run_id); lifelong facts/directives are
 * shared across every run.
 */

import { describe, it, expect } from "vitest";

import { execute } from "../../src/exec/executor.js";
import { buildBasicPlugins } from "../../src/plugins/index.js";
import { InProcessStore } from "../../src/exec/store.js";
import { assembleRecall } from "../../src/exec/recall.js";
import type { RotorDocument } from "../../src/types.js";

const doc = (name: string, steps: unknown[]): RotorDocument =>
  ({ apiVersion: "rotor.glyphh.ai/v0.1", kind: "Rotor", metadata: { name, version: "0.1.0" }, spec: { entry: (steps[0] as { id: string }).id, steps } }) as unknown as RotorDocument;

// Definition "base": absorbs a standing directive + a self-fact.
const base = doc("base", [
  { id: "w", type: "write", in: { text: "Always use tabs for indentation. My name is Ada." }, out: {}, config: { mode: "absorb", key: "user" }, next: "end" },
]);

// A DIFFERENT definition, "super-power": recalls the user's directives via a
// closed-op lookup — no knowledge of what wrote them.
const superPower = doc("super-power", [
  { id: "r", type: "retrieve.sql", in: { person: "user", slot: "directive" }, out: {}, config: { op: "lookup" }, next: "end" },
]);

describe("one runtime = one stator, shared across definitions", () => {
  it("facts written by 'base' are recalled by 'super-power' on a later run", async () => {
    // A single stator stands in for one runtime instance.
    const store = new InProcessStore();

    // Run 1: the base definition writes memory.
    const r1 = await execute(base, {}, buildBasicPlugins({ store }));
    // Run 2: a different definition, later, against the SAME stator.
    const r2 = await execute(superPower, {}, buildBasicPlugins({ store }));

    // super-power recalls base's directive though it never wrote it.
    const rows = (r2.history.find((h) => h.step_id === "r")?.output as { rows: Array<{ filler: string }> }).rows;
    expect(rows.some((row) => /always use tabs/i.test(row.filler))).toBe(true);

    // The self-fact is there too, retrievable by any later definition.
    const ctx = await assembleRecall(buildBasicPlugins({ store }).memory, "");
    expect(ctx.directives.join(" ")).toMatch(/tabs/i);

    // Run history is PER-RUN (distinct run ids, distinct tapes); memory is shared.
    expect(r1.run_id).not.toBe(r2.run_id);
    expect((await store.history.read(r1.run_id)).length).toBeGreaterThan(0);
    expect((await store.history.read(r2.run_id)).length).toBeGreaterThan(0);
  });

  it("a fresh stator (a different runtime) starts empty — memory is per-runtime", async () => {
    const otherRuntime = new InProcessStore();
    const r = await execute(superPower, {}, buildBasicPlugins({ store: otherRuntime }));
    const rows = (r.history.find((h) => h.step_id === "r")?.output as { rows: unknown[] }).rows;
    expect(rows).toHaveLength(0); // no memory carried over from another runtime's stator
  });
});
