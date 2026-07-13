/**
 * HDC grounding integration (BUILD_PLAN.md Phase 6): associative probe/verify with
 * real margins, the hard grounding gate on a `model` step (§6.3), and the
 * `retrieve.kb` entity-graph modes (node/neighbors). Grounding is deterministic, so
 * repeated probes are identical and the recorded run replays.
 */

import { describe, it, expect } from "vitest";

import { execute } from "../../src/exec/executor.js";
import { buildBasicPlugins } from "../../src/plugins/index.js";
import { InProcessStore } from "../../src/exec/store.js";
import type { RotorDocument } from "../../src/types.js";

const doc = (spec: Record<string, unknown>): RotorDocument =>
  ({ apiVersion: "rotor.glyphh.ai/v0.1", kind: "Rotor", metadata: { name: "g", version: "0.1.0" }, spec }) as unknown as RotorDocument;

/** A store seeded with a small fact graph: ada & bob both live in london; carol
 *  lives in paris; bob has a cat. ada carries several facts so its cortex is a
 *  real bundle (superposition noise → a meaningful, sub-1 recall margin). */
async function seeded() {
  const store = new InProcessStore();
  const plugins = buildBasicPlugins({ store });
  await plugins.memory.write(
    [
      { entity: "ada", role: "rel.city", filler: "london" },
      { entity: "ada", role: "rel.pet", filler: "dog" },
      { entity: "ada", role: "rel.job", filler: "engineer" },
      { entity: "bob", role: "rel.city", filler: "london" },
      { entity: "carol", role: "rel.city", filler: "paris" },
      { entity: "bob", role: "rel.pet", filler: "cat" },
    ],
    { tick: 0 },
  );
  return { store, plugins };
}

describe("HDC probe / verify", () => {
  it("recalls the grounded filler with a positive margin", async () => {
    const { plugins } = await seeded();
    const p = await plugins.grounding.probe("ada", "rel.city");
    expect(p.filler).toBe("london");
    expect(p.margin).toBeGreaterThan(0);
    // Deterministic: a second probe is identical.
    expect(await plugins.grounding.probe("ada", "rel.city")).toEqual(p);
  });

  it("grounds a claim that clears the margin threshold", async () => {
    const { plugins } = await seeded();
    expect((await plugins.grounding.verify("ada", "rel.city", "london", 0.02)).grounded).toBe(true);
  });

  it("refuses when the margin threshold is not met (low confidence)", async () => {
    const { plugins } = await seeded();
    // A threshold above any achievable margin gates the claim out.
    expect((await plugins.grounding.verify("ada", "rel.city", "london", 0.99)).grounded).toBe(false);
  });

  it("refuses a claim that is not in the grounded set", async () => {
    const { plugins } = await seeded();
    expect((await plugins.grounding.verify("ada", "rel.city", "berlin", 0.02)).grounded).toBe(false);
  });
});

describe("hard grounding gate on a model step (§6.3)", () => {
  const modelDoc = (entity: string) =>
    doc({
      entry: "m",
      steps: [
        { id: "m", type: "model", in: { entity }, out: {}, config: { ground: { entity, role: "rel.city", enforcement: "hard" }, lane: "local" }, next: "end" },
      ],
    });

  it("decodes into a grounded filler when one exists", async () => {
    const { store } = await seeded();
    const r = await execute(modelDoc("ada"), {}, buildBasicPlugins({ store }));
    expect(r.status).toBe("ok");
    const m = r.history.find((h) => h.step_id === "m");
    expect(String((m?.output as { text?: string }).text)).toContain("london");
  });

  it("refuses when there is no grounded continuation", async () => {
    const { store } = await seeded();
    const r = await execute(modelDoc("zoe"), {}, buildBasicPlugins({ store }));
    const m = r.history.find((h) => h.step_id === "m");
    expect(m?.status).toBe("refused");
    expect((m?.output as { reason?: string }).reason).toBe("E_UNGROUNDED");
  });
});

describe("retrieve.kb entity graph", () => {
  const kbDoc = (mode: string) =>
    doc({
      entry: "k",
      steps: [{ id: "k", type: "retrieve.kb", in: { entity: "ada" }, out: {}, config: { mode, entity: "ada", role: "rel.city" }, next: "end" }],
    });

  it("node returns the entity's edges", async () => {
    const { store } = await seeded();
    const r = await execute(kbDoc("node"), {}, buildBasicPlugins({ store }));
    const out = r.history.find((h) => h.step_id === "k")?.output as { edges: Array<{ filler: string }> };
    expect(out.edges.some((e) => e.filler === "london")).toBe(true);
  });

  it("neighbors returns co-referent entities (shared filler)", async () => {
    const { store } = await seeded();
    const r = await execute(kbDoc("neighbors"), {}, buildBasicPlugins({ store }));
    const out = r.history.find((h) => h.step_id === "k")?.output as { neighbors: string[] };
    // ada shares 'london' with bob, not with carol.
    expect(out.neighbors).toContain("bob");
    expect(out.neighbors).not.toContain("carol");
    expect(out.neighbors).not.toContain("ada");
  });
});
