/**
 * Model prompt-cache + micro-rotor integration (BUILD_PLAN.md Phase 9). The micro
 * rotor (§6.3) backtracks over the role vocabulary bounded by `max_backtracks`;
 * prompt caching (§8.6) populates cache_write/cache_read usage without changing the
 * decoded output.
 */

import { describe, it, expect } from "vitest";

import { execute } from "../../src/exec/executor.js";
import { buildBasicPlugins } from "../../src/plugins/index.js";
import { BasicModels } from "../../src/plugins/models.js";
import { InProcessStore } from "../../src/exec/store.js";
import { tokenish } from "../../src/exec/util.js";
import type { ModelRequest, ModelResult, ModelsPlugin, Plugins } from "../../src/plugins/interfaces.js";
import type { RotorDocument } from "../../src/types.js";

/** A models plugin whose local lane actually answers (no `stub` frame), so model
 *  steps are durable and prompt-cache accounting can be asserted off the tape. The
 *  bare BasicModels would degrade to the stub here, which is intentionally
 *  non-cacheable (see stub-no-replay.test.ts). */
class LiveModels extends BasicModels {
  async execute(request: ModelRequest): Promise<ModelResult> {
    const text = `echo: ${request.prompt}`;
    return {
      text,
      served: "local",
      frames: [{ type: "propose", data: { text } }, { type: "done" }],
      usage: { input: tokenish(request.prompt), output: tokenish(text), cost: 0 },
    };
  }
}

function livePlugins(store: InProcessStore): Plugins {
  const plugins = buildBasicPlugins({ store });
  (plugins as { models: ModelsPlugin }).models = new LiveModels();
  return plugins;
}

/** ada's grounded city is `zebra`; bob/carol give the role vocabulary distractors
 *  that sort before it (apple, mango, zebra) so the micro rotor must backtrack. */
function seeded() {
  const store = new InProcessStore();
  buildBasicPlugins({ store }).memory.write(
    [
      { entity: "ada", role: "c", filler: "zebra" },
      { entity: "bob", role: "c", filler: "apple" },
      { entity: "carol", role: "c", filler: "mango" },
    ],
    { tick: 0 },
  );
  return store;
}

const microDoc = (maxBacktracks: number): RotorDocument =>
  ({
    apiVersion: "rotor.glyphh.ai/v0.1",
    kind: "Rotor",
    metadata: { name: "m", version: "0.1.0" },
    spec: {
      entry: "m",
      steps: [
        {
          id: "m",
          type: "model",
          in: { entity: "ada", text: "which city" },
          out: {},
          config: { ground: { entity: "ada", role: "c", enforcement: "hard" }, micro: true, max_backtracks: maxBacktracks, cache: { breakpoints: ["system"] } },
          next: "end",
        },
      ],
    },
  }) as unknown as RotorDocument;

describe("micro rotor (§6.3)", () => {
  it("backtracks to the grounded winner within max_backtracks", async () => {
    const r = await execute(microDoc(5), {}, buildBasicPlugins({ store: seeded() }));
    const m = r.history.find((h) => h.step_id === "m");
    expect(m?.status).toBe("ok");
    expect((m?.output as { text: string }).text).toBe("zebra");
    // apple, mango rejected → 2 backtrack frames before the assert.
    expect((m?.frames ?? []).filter((f) => f.type === "backtrack")).toHaveLength(2);
  });

  it("refuses when the winner is beyond max_backtracks", async () => {
    const r = await execute(microDoc(1), {}, buildBasicPlugins({ store: seeded() }));
    const m = r.history.find((h) => h.step_id === "m");
    expect(m?.status).toBe("refused");
    expect((m?.output as { reason: string }).reason).toBe("E_UNGROUNDED");
  });
});

describe("prompt caching (§8.6) — usage only, output unchanged", () => {
  const twoModelDoc: RotorDocument = {
    apiVersion: "rotor.glyphh.ai/v0.1",
    kind: "Rotor",
    metadata: { name: "pc", version: "0.1.0" },
    spec: {
      entry: "m1",
      steps: [
        { id: "m1", type: "model", in: { text: "first turn" }, out: {}, config: { cache: { breakpoints: ["system"] } }, next: "m2" },
        { id: "m2", type: "model", in: { text: "second turn" }, out: {}, config: { cache: { breakpoints: ["system"] } }, next: "end" },
      ],
    },
  } as unknown as RotorDocument;

  it("writes the prefix on the first model call and hits on the second", async () => {
    const r = await execute(twoModelDoc, {}, livePlugins(new InProcessStore()));
    const m1 = r.history.find((h) => h.step_id === "m1");
    const m2 = r.history.find((h) => h.step_id === "m2");
    expect(m1?.usage?.cache_write).toBeGreaterThan(0);
    expect(m1?.usage?.cache_read).toBe(0);
    expect(m2?.usage?.cache_read).toBeGreaterThan(0);
    expect(m2?.usage?.cache_write).toBe(0);
    // Each model step emits a cache disposition frame.
    expect((m1?.frames ?? []).some((f) => f.type === "cache")).toBe(true);
  });
});
