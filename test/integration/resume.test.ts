/**
 * wait / interrupt resume integration (BUILD_PLAN.md Phase 7): a run pauses at a
 * `wait`/`approval` step, checkpoints the completed prefix, and resumes with an
 * injected payload to completion — deterministically. `on_timeout` routes as
 * declared; an `approval` gate resolves on the decision.
 */

import { describe, it, expect } from "vitest";

import { execute } from "../../src/exec/executor.js";
import { buildBasicPlugins } from "../../src/plugins/index.js";
import { InProcessStore } from "../../src/exec/store.js";
import type { RotorDocument } from "../../src/types.js";

const waitDoc: RotorDocument = {
  apiVersion: "rotor.glyphh.ai/v0.1",
  kind: "Rotor",
  metadata: { name: "w", version: "0.1.0" },
  spec: {
    entry: "a",
    steps: [
      { id: "a", type: "transform", in: {}, out: {}, config: { set: { step: "a" } }, next: "gate" },
      { id: "gate", type: "wait", in: {}, out: {}, config: { on: "approval", on_timeout: "fail" }, next: "done" },
      { id: "done", type: "transform", in: {}, out: {}, config: { set: { final: "complete" } }, next: "end" },
    ],
  },
} as unknown as RotorDocument;

describe("wait → resume", () => {
  it("pauses at the wait step and checkpoints only the completed prefix", async () => {
    const r = await execute(waitDoc, {}, buildBasicPlugins());
    expect(r.status).toBe("interrupted");
    expect(r.terminal).toBe("gate");
    expect(r.interrupt).toMatchObject({ stepId: "gate", awaiting: "approval" });
    // The paused step is NOT recorded — only `a` completed.
    expect(r.history.map((h) => h.step_id)).toEqual(["a"]);
  });

  it("resumes with a payload and runs to completion", async () => {
    const store = new InProcessStore();
    const paused = await execute(waitDoc, {}, buildBasicPlugins({ store }));
    const resumed = await execute(waitDoc, {}, buildBasicPlugins({ store }), {
      runId: paused.run_id,
      resume: { stepId: "gate", payload: { value: "ok" } },
    });
    expect(resumed.status).toBe("ok");
    expect(resumed.terminal).toBe("end");
    expect(resumed.history.map((h) => h.step_id)).toEqual(["a", "gate", "done"]);
  });

  it("resume is deterministic (two resumes from the paused prefix match)", async () => {
    const run = async () => {
      const store = new InProcessStore();
      const paused = await execute(waitDoc, {}, buildBasicPlugins({ store }));
      const resumed = await execute(waitDoc, {}, buildBasicPlugins({ store }), {
        runId: paused.run_id,
        resume: { stepId: "gate", payload: { value: "ok" } },
      });
      return resumed.history.map((h) => ({ step: h.step_id, tick: h.logical_tick, status: h.status }));
    };
    expect(await run()).toEqual(await run());
  });

  it("routes via on_timeout when resumed with a timeout", async () => {
    const store = new InProcessStore();
    const paused = await execute(waitDoc, {}, buildBasicPlugins({ store }));
    const resumed = await execute(waitDoc, {}, buildBasicPlugins({ store }), {
      runId: paused.run_id,
      resume: { stepId: "gate", timeout: true },
    });
    expect(resumed.status).toBe("failed");
    expect(resumed.terminal).toBe("__fail__");
  });
});

describe("approval gate → resume", () => {
  const approvalDoc: RotorDocument = {
    apiVersion: "rotor.glyphh.ai/v0.1",
    kind: "Rotor",
    metadata: { name: "ap", version: "0.1.0" },
    spec: {
      entry: "g",
      steps: [
        { id: "g", type: "gate", in: {}, out: {}, config: { mode: "approval", on_pass: "yes", on_fail: "no" } },
        { id: "yes", type: "transform", in: {}, out: {}, config: { set: { path: "approved" } }, next: "end" },
        { id: "no", type: "transform", in: {}, out: {}, config: { set: { path: "rejected" } }, next: "end" },
      ],
    },
  } as unknown as RotorDocument;

  it("pauses, then routes on the decision", async () => {
    const approve = async (decision: string) => {
      const store = new InProcessStore();
      const paused = await execute(approvalDoc, {}, buildBasicPlugins({ store }));
      expect(paused.status).toBe("interrupted");
      const resumed = await execute(approvalDoc, {}, buildBasicPlugins({ store }), {
        runId: paused.run_id,
        resume: { stepId: "g", payload: { decision } },
      });
      return resumed.history.map((h) => h.step_id);
    };
    expect(await approve("approve")).toContain("yes");
    expect(await approve("reject")).toContain("no");
  });
});
