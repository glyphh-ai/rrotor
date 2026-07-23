/**
 * Fallback turns are non-cacheable (§9 graceful degradation is per-run, not sticky).
 *
 * A turn whose answer came from the zero-model STUB (a `stub` frame — no bound model)
 * or a DEGRADED lane (a `degrade` frame) must NOT be checkpointed to the replay tape:
 * once the user binds a model, re-asking the same prompt (same content-addressed run
 * id) must re-run against the real model, not replay the stale fallback. This is the
 * bug that poisoned the glyphh CLI's cache with dozens of pre-fix "hi" turns.
 */

import { describe, it, expect } from "vitest";

import { execute } from "../../src/exec/executor.js";
import { buildBasicPlugins } from "../../src/plugins/index.js";
import { InProcessStore } from "../../src/exec/store.js";
import type { StepHandler } from "../../src/handlers/index.js";
import type { RotorDocument, StepResult } from "../../src/types.js";

/** A minimal conversational rotor: a deterministic `prompt` feeding a `model` step. */
const chatDoc: RotorDocument = {
  apiVersion: "rotor.glyphh.ai/v0.1",
  kind: "Rotor",
  metadata: { name: "chat", version: "0.1.0" },
  spec: {
    entry: "p",
    steps: [
      { id: "p", type: "prompt", in: { text: "$.inputs.q" }, out: {}, config: { template: "{{text}}" }, next: "m" },
      { id: "m", type: "model", in: { text: "$.steps.p.text" }, out: {}, config: {}, next: "end" },
    ],
  },
} as unknown as RotorDocument;

/** A test model handler that answers from the stub (when unbound) or a "real" model
 *  (once bound), counting how many times it is actually invoked. */
function fakeModel() {
  let bound = false;
  let calls = 0;
  const handler: StepHandler = {
    type: "model",
    async execute({ input }): Promise<StepResult> {
      calls++;
      const prompt = String(input.text ?? "");
      if (!bound) {
        // Mirror BasicModels.stub(): a `stub` frame marks the zero-model fallback.
        const text = `[stub:local] ${prompt}`;
        return { output: { text }, frames: [{ type: "stub", data: { lane: "local" } }, { type: "propose", data: { text } }, { type: "done" }], status: "ok" };
      }
      const text = `real answer to: ${prompt}`;
      return { output: { text, served: "local" }, frames: [{ type: "propose", data: { text } }, { type: "done" }], status: "ok" };
    },
  };
  return { handler, bind: () => { bound = true; }, calls: () => calls };
}

describe("fallback turns are non-cacheable (§9)", () => {
  it("does not checkpoint the model step when the answer came from the stub", async () => {
    const store = new InProcessStore();
    const m = fakeModel();
    const r = await execute(chatDoc, { q: "hi" }, buildBasicPlugins({ store }), { handlers: { model: m.handler } });

    // The current turn still completes with the stub answer...
    expect(r.status).toBe("ok");
    expect((r.outputs as Record<string, unknown>) ?? {}).toBeDefined();

    // ...but the model step is absent from the replay tape (the deterministic prompt
    // step, recorded before the fallback, stays).
    const recorded = (await store.history.read(r.run_id)).map((h) => h.step_id);
    expect(recorded).toContain("p");
    expect(recorded).not.toContain("m");
  });

  it("re-runs the model on a repeat prompt once a model is bound (no stub replay)", async () => {
    const store = new InProcessStore();
    const m = fakeModel();

    // Turn 1: unbound → stub. Same inputs ⇒ same content-addressed run id as turn 2.
    const first = await execute(chatDoc, { q: "hi" }, buildBasicPlugins({ store }), { handlers: { model: m.handler } });
    expect((first.outputs as { answer?: string })).toBeDefined();
    expect(m.calls()).toBe(1);
    const firstText = (first.context.steps.m as { text: string }).text;
    expect(firstText).toBe("[stub:local] hi");

    // User binds a model. Turn 2: SAME prompt, same run id — must hit the model, not replay.
    m.bind();
    const second = await execute(chatDoc, { q: "hi" }, buildBasicPlugins({ store }), {
      runId: first.run_id,
      handlers: { model: m.handler },
    });

    // The model handler was invoked a second time (a real model call, not a 0-step replay)...
    expect(m.calls()).toBe(2);
    // ...and the turn now carries the real answer, which IS checkpointed for future replay.
    const secondText = (second.context.steps.m as { text: string }).text;
    expect(secondText).toBe("real answer to: hi");
    const recorded = (await store.history.read(second.run_id)).map((h) => h.step_id);
    expect(recorded).toContain("m");
  });

  it("DOES checkpoint a grounded stub decode (deterministic ranker is replay-safe)", async () => {
    const store = new InProcessStore();
    let calls = 0;
    // A stub frame carrying `grounded: true` is the zero-model ranker, not the bare
    // echo — it is durable, so it must be recorded and replayed like a real answer.
    const groundedStub: StepHandler = {
      type: "model",
      async execute({ input }): Promise<StepResult> {
        calls++;
        const text = String(input.text ?? "");
        return { output: { text }, frames: [{ type: "stub", data: { lane: "local", grounded: true } }, { type: "propose", data: { text } }, { type: "done" }], status: "ok" };
      },
    };
    const first = await execute(chatDoc, { q: "hi" }, buildBasicPlugins({ store }), { runId: "run-grounded", handlers: { model: groundedStub } });
    expect(calls).toBe(1);
    expect((await store.history.read(first.run_id)).map((h) => h.step_id)).toContain("m");

    // Same run id → pure replay, no second call.
    await execute(chatDoc, { q: "hi" }, buildBasicPlugins({ store }), { runId: "run-grounded", handlers: { model: groundedStub } });
    expect(calls).toBe(1);
  });

  it("replays the real answer on a third identical prompt (fresh answers ARE cached)", async () => {
    const store = new InProcessStore();
    const m = fakeModel();
    m.bind();

    const first = await execute(chatDoc, { q: "hi" }, buildBasicPlugins({ store }), { runId: "run-fixed", handlers: { model: m.handler } });
    expect(m.calls()).toBe(1);
    expect((first.context.steps.m as { text: string }).text).toBe("real answer to: hi");

    // Same run id, bound model already recorded → pure replay, no second model call.
    const second = await execute(chatDoc, { q: "hi" }, buildBasicPlugins({ store }), { runId: "run-fixed", handlers: { model: m.handler } });
    expect(m.calls()).toBe(1);
    expect((second.context.steps.m as { text: string }).text).toBe("real answer to: hi");
  });
});
