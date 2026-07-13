/**
 * Trust-layer integration (BUILD_PLAN.md Phase 5): E_UNMERGEABLE on undeclared
 * concurrent writes (§5.2), sub-rotor identity attenuation (§11.3), and
 * spec.access field redaction from outputs AND drain envelopes (§13.4).
 */

import { describe, it, expect } from "vitest";

import { execute } from "../../src/exec/executor.js";
import { buildBasicPlugins } from "../../src/plugins/index.js";
import { InProcessStore } from "../../src/exec/store.js";
import { toEnvelope } from "../../src/plugins/drain.js";
import type { DrainEnvelope, DrainPlugin } from "../../src/plugins/interfaces.js";
import type { RotorDocument, StepRecord } from "../../src/types.js";

const doc = (spec: Record<string, unknown>, meta: Record<string, unknown> = {}): RotorDocument =>
  ({
    apiVersion: "rotor.glyphh.ai/v0.1",
    kind: "Rotor",
    metadata: { name: "t", version: "0.1.0", ...meta },
    spec,
  }) as unknown as RotorDocument;

class CapturingDrain implements DrainPlugin {
  readonly name = "drain";
  envelopes: DrainEnvelope[] = [];
  emit(r: StepRecord): void {
    this.envelopes.push(toEnvelope(r));
  }
  async flush(): Promise<void> {}
  async close(): Promise<void> {}
  status() {
    return { ready: true as const, detail: "capture", tier: "basic" as const };
  }
}

describe("E_UNMERGEABLE (§5.2)", () => {
  const parallelDoc = (reducer?: unknown) =>
    doc({
      entry: "p",
      steps: [
        { id: "p", type: "parallel", in: {}, out: {}, config: { mode: "parallel", branches: ["x", "y"], reducer }, next: "end" },
        { id: "x", type: "transform", in: {}, out: {}, config: { set: { k: "from-x" } }, next: "end" },
        { id: "y", type: "transform", in: {}, out: {}, config: { set: { k: "from-y" } }, next: "end" },
      ],
    });

  it("fails a concurrent write to a key with no declared reducer", async () => {
    const r = await execute(parallelDoc(undefined), {}, buildBasicPlugins());
    expect(r.status).toBe("failed");
    const p = r.history.find((h) => h.step_id === "p");
    expect(p?.error?.name).toBe("E_UNMERGEABLE");
  });

  it("succeeds when a reducer is declared", async () => {
    const r = await execute(parallelDoc("last-write-wins"), {}, buildBasicPlugins());
    const p = r.history.find((h) => h.step_id === "p");
    expect(p?.status).toBe("ok");
    expect(p?.error).toBeUndefined();
  });
});

describe("sub-rotor identity attenuation (§11.3)", () => {
  const child = (scopes: string[]) =>
    doc(
      {
        identity: { scopes },
        entry: "c",
        steps: [{ id: "c", type: "transform", in: {}, out: {}, config: { set: { done: true } }, next: "end" }],
      },
      { name: "child" },
    );
  const parent = doc({
    entry: "s",
    steps: [{ id: "s", type: "sub-rotor", in: {}, out: {}, config: { ref: "child" }, next: "end" }],
  });
  const caller = { id: "u", kind: "user" as const, scopes: ["scope:a", "scope:b"] };

  it("runs the callee under the INTERSECTION of scopes", async () => {
    const store = new InProcessStore();
    const resolver = () => child(["scope:a"]);
    const r = await execute(parent, {}, buildBasicPlugins({ store }), { principal: caller, rotorResolver: resolver });
    expect(r.status).toBe("ok");
    // The sub-run's records carry the narrowed identity.
    const subRecords = await store.history.read(`${r.run_id}::child`);
    expect(subRecords.length).toBeGreaterThan(0);
    expect(subRecords[0].principal?.scopes).toEqual(["scope:a"]);
  });

  it("refuses when the callee requests a scope the caller lacks", async () => {
    const resolver = () => child(["scope:c"]);
    const r = await execute(parent, {}, buildBasicPlugins(), { principal: caller, rotorResolver: resolver });
    const s = r.history.find((h) => h.step_id === "s");
    expect(s?.status).toBe("refused");
    expect(s?.output).toMatchObject({ refused: "E_SCOPE_EXCEEDED" });
  });
});

describe("spec.access field redaction (§13.4)", () => {
  const redactingDoc = doc({
    access: { redact: ["secret"], on_ungranted: "redact" },
    entry: "t",
    steps: [{ id: "t", type: "transform", in: {}, out: {}, config: { set: { answer: "42", secret: "hunter2" } }, next: "end" }],
  });

  it("strips redacted fields from the recorded output", async () => {
    const r = await execute(redactingDoc, {}, buildBasicPlugins());
    const rec = r.history.find((h) => h.step_id === "t");
    expect(rec?.output).toEqual({ answer: "42" });
    expect(rec?.output).not.toHaveProperty("secret");
  });

  it("keeps redacted fields out of drain envelopes too", async () => {
    const drain = new CapturingDrain();
    await execute(redactingDoc, {}, buildBasicPlugins({ drain }));
    const env = drain.envelopes.find((e) => e.step_id === "t");
    expect(env?.output).toEqual({ answer: "42" });
    expect(env?.output).not.toHaveProperty("secret");
  });
});
