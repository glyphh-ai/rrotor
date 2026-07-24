/**
 * Turn interruption (a user Esc-stop): an aborted `signal` threaded into
 * `execute` stops the run cooperatively — the loop ends `interrupted` at the
 * next step boundary, recording the pause cursor, and no answer is projected.
 * Same seam the TUI drives from Esc and the `model` step forwards to the
 * provider fetch.
 */

import { describe, it, expect } from "vitest";

import { execute } from "../../src/exec/executor.js";
import { buildBasicPlugins } from "../../src/plugins/index.js";
import { InProcessStore } from "../../src/exec/store.js";
import type { RotorDocument } from "../../src/types.js";

const doc = (spec: Record<string, unknown>): RotorDocument =>
  ({ apiVersion: "rotor.glyphh.ai/v0.1", kind: "Rotor", metadata: { name: "abort", version: "0.1.0" }, spec }) as unknown as RotorDocument;

/** Two plain transform steps: a → b → end. Deterministic, no model needed. */
const twoStep = () =>
  doc({
    entry: "a",
    steps: [
      { id: "a", type: "transform", in: {}, out: { x: "x" }, config: { set: { x: 1 } }, next: "b" },
      { id: "b", type: "transform", in: {}, out: { y: "y" }, config: { set: { y: 2 } }, next: "end" },
    ],
  });

describe("turn interruption (user Esc-stop)", () => {
  it("an already-aborted signal ends the run interrupted at the entry step", async () => {
    const store = new InProcessStore();
    const controller = new AbortController();
    controller.abort();
    const r = await execute(twoStep(), {}, buildBasicPlugins({ store }), { signal: controller.signal });
    expect(r.status).toBe("interrupted");
    expect(r.terminal).toBe("a");
    expect(r.interrupt).toEqual({ stepId: "a", awaiting: { reason: "aborted" } });
    // The check fires BEFORE the first step runs — nothing executed.
    expect(r.history.length).toBe(0);
  });

  it("aborting after the first step stops at the next boundary", async () => {
    const store = new InProcessStore();
    const controller = new AbortController();
    // Abort as soon as the first step's record lands on the drain, so the loop's
    // next-iteration check trips deterministically after exactly one step.
    let seen = 0;
    const plugins = buildBasicPlugins({
      store,
      drain: {
        name: "drain",
        emit: () => {
          if (++seen === 1) controller.abort();
        },
        flush: async () => {},
        close: async () => {},
        status: () => ({ ready: true, detail: "test", tier: "basic" as const }),
      },
    });
    const r = await execute(twoStep(), {}, plugins, { signal: controller.signal });
    expect(r.status).toBe("interrupted");
    expect(r.terminal).toBe("b");
    expect(r.interrupt).toEqual({ stepId: "b", awaiting: { reason: "aborted" } });
    expect(r.history.length).toBe(1); // only step a ran
  });

  it("without a signal the same run completes normally", async () => {
    const store = new InProcessStore();
    const r = await execute(twoStep(), {}, buildBasicPlugins({ store }));
    expect(r.status).toBe("ok");
    expect(r.terminal).toBe("end");
    expect(r.interrupt).toBeUndefined();
  });
});
