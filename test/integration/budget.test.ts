/**
 * Attention-budget + retry-backoff integration (BUILD_PLAN.md Phase 4). Proves the
 * §10 budget terminates a run at its deterministic bound (and replays identically),
 * that `on_exhausted: escalate` routes to the escalate step, and that §5.5 retry
 * backoff is honored on fresh execution but never on replay.
 */

import { describe, it, expect } from "vitest";

import { execute } from "../../src/exec/executor.js";
import { buildBasicPlugins } from "../../src/plugins/index.js";
import { InProcessStore } from "../../src/exec/store.js";
import { checkDeterminism, checkReplay } from "../harness/replay.js";
import { loadFixture } from "../harness/fixtures.js";
import type { RotorDocument, StepResult } from "../../src/types.js";

const looping = loadFixture("test/fixtures/looping.rotor.yaml");

describe("attention budget — enforcement", () => {
  it("stops at the revolutions bound with a deterministic budget outcome", async () => {
    const r = await execute(looping, { loop: "yes" }, buildBasicPlugins());
    expect(r.terminal).toBe("__budget__");
    expect(r.budget).toMatchObject({ on: "stop", reason: "revolutions", revolutions: 3 });
  });

  it("routes to the escalate step when on_exhausted = escalate (§9.1)", async () => {
    const doc = structuredClone(looping);
    doc.spec.attention!.budget!.on_exhausted = "escalate";
    const r = await execute(doc, { loop: "yes" }, buildBasicPlugins());
    expect(r.history.map((h) => h.step_id)).toContain("esc");
    expect(r.terminal).toBe("end");
    expect(r.budget?.reason).toBe("revolutions");
  });

  it("is deterministic and replays identically", async () => {
    const det = await checkDeterminism(looping, { loop: "yes" });
    expect(det.identical).toBe(true);
    const rep = await checkReplay(looping, { loop: "yes" });
    expect(rep.identical).toBe(true);
    expect(rep.appendedDuringReplay).toBe(0);
  });

  it("leaves a rotor with no budget unaffected", async () => {
    // The base rotor declares revolutions:4/escalate but does not loop under the
    // stub evaluator, so the budget never triggers.
    const base = loadFixture("rotors/base.rotor.yaml");
    const r = await execute(base, { prompt: "hi" }, buildBasicPlugins());
    expect(r.terminal).not.toBe("__budget__");
    expect(r.budget).toBeUndefined();
  });
});

describe("retry backoff (§5.5)", () => {
  // A one-step rotor whose handler fails twice then succeeds; execute() does not
  // require schema validation, so a minimal literal document suffices.
  const flaky: RotorDocument = {
    apiVersion: "rotor.glyphh.ai/v0.1",
    kind: "Rotor",
    metadata: { name: "flaky", version: "0.1.0" },
    spec: {
      entry: "t",
      steps: [
        {
          id: "t",
          type: "transform",
          in: {},
          out: {},
          config: { set: {} },
          retry: [{ errors: ["E_FLAKY"], max_attempts: 3, interval_ms: 10, backoff_rate: 2 }],
          next: "end",
        },
      ],
    },
  } as unknown as RotorDocument;

  function flakyHandler() {
    let calls = 0;
    return {
      calls: () => calls,
      handler: {
        type: "transform" as const,
        execute: async (): Promise<StepResult> => {
          calls++;
          if (calls <= 2) {
            const e = new Error("flaky");
            e.name = "E_FLAKY";
            throw e;
          }
          return { output: { ok: true }, frames: [], status: "ok" };
        },
      },
    };
  }

  it("sleeps interval_ms * backoff_rate^n between fresh retries, then succeeds", async () => {
    const sleeps: number[] = [];
    const { calls, handler } = flakyHandler();
    const r = await execute(flaky, {}, buildBasicPlugins(), {
      handlers: { transform: handler },
      sleep: async (ms) => {
        sleeps.push(ms);
      },
    });
    expect(r.status).toBe("ok");
    expect(calls()).toBe(3); // failed twice, succeeded on the third
    expect(sleeps).toEqual([10, 20]); // 10*2^0, 10*2^1
  });

  it("never sleeps on replay", async () => {
    const store = new InProcessStore();
    const first = flakyHandler();
    const firstSleeps: number[] = [];
    await execute(flaky, {}, buildBasicPlugins({ store }), {
      handlers: { transform: first.handler },
      sleep: async (ms) => {
        firstSleeps.push(ms);
      },
    });
    expect(firstSleeps).toEqual([10, 20]);

    // Replay against the shared store: records exist, so the handler is never
    // called and no backoff sleeps happen.
    const second = flakyHandler();
    const replaySleeps: number[] = [];
    const r = await execute(flaky, {}, buildBasicPlugins({ store }), {
      handlers: { transform: second.handler },
      sleep: async (ms) => {
        replaySleeps.push(ms);
      },
    });
    expect(replaySleeps).toEqual([]);
    expect(second.calls()).toBe(0);
    expect(r.status).toBe("ok");
  });
});
