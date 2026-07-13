/**
 * Run-pinning + patch gates (BUILD_PLAN.md Phase 8, SPEC §16.3). A run records its
 * `definitionVersion`; replaying it against an edited document fails with
 * `E_REPLAY_DIVERGENCE` unless the document declares the recorded version in
 * `spec.patch` — the Temporal patch model, so old and new definitions coexist.
 */

import { describe, it, expect } from "vitest";

import { execute } from "../../src/exec/executor.js";
import { buildBasicPlugins } from "../../src/plugins/index.js";
import { InProcessStore } from "../../src/exec/store.js";
import type { RotorDocument } from "../../src/types.js";

const mk = (version: string, patch?: string[]): RotorDocument =>
  ({
    apiVersion: "rotor.glyphh.ai/v0.1",
    kind: "Rotor",
    metadata: { name: "p", version },
    spec: {
      ...(patch ? { patch } : {}),
      entry: "a",
      steps: [
        { id: "a", type: "transform", in: {}, out: {}, config: { set: { x: 1 } }, next: "b" },
        { id: "b", type: "transform", in: {}, out: {}, config: { set: { y: 2 } }, next: "end" },
      ],
    },
  }) as unknown as RotorDocument;

async function recordV1() {
  const store = new InProcessStore();
  const v1 = mk("0.1.0");
  const run = await execute(v1, {}, buildBasicPlugins({ store }));
  return { store, runId: run.run_id };
}

describe("run-pinning + patch gates (§16.3)", () => {
  it("records the definitionVersion on every StepRecord", async () => {
    const { store, runId } = await recordV1();
    for (const rec of await store.history.read(runId)) expect(rec.definitionVersion).toBe("0.1.0");
  });

  it("replaying against an edited version fails with E_REPLAY_DIVERGENCE", async () => {
    const { store, runId } = await recordV1();
    const r = await execute(mk("0.2.0"), {}, buildBasicPlugins({ store }), { runId });
    expect(r.status).toBe("failed");
    expect(r.terminal).toBe("__fail__");
    expect(r.error?.name).toBe("E_REPLAY_DIVERGENCE");
  });

  it("replays cleanly when the edited version declares the recorded version in spec.patch", async () => {
    const { store, runId } = await recordV1();
    const r = await execute(mk("0.2.0", ["0.1.0"]), {}, buildBasicPlugins({ store }), { runId });
    expect(r.status).toBe("ok");
    expect(r.terminal).toBe("end");
    expect(r.history.map((h) => h.step_id)).toEqual(["a", "b"]);
  });

  it("replaying against the same version is always clean", async () => {
    const { store, runId } = await recordV1();
    const r = await execute(mk("0.1.0"), {}, buildBasicPlugins({ store }), { runId });
    expect(r.status).toBe("ok");
  });
});
