/**
 * E2 — error-handling discipline, end to end: a handler that throws a RotorError
 * has its taxonomy code survive to the recorded tape AND the drain envelope is
 * enriched with category/severity/retryable/remediation. A raw (non-taxonomy)
 * throw is normalized to E_HANDLER. Determinism is untouched: the tape carries
 * only {name, cause}.
 */

import { describe, it, expect } from "vitest";

import { execute } from "../../src/exec/executor.js";
import { buildBasicPlugins } from "../../src/plugins/index.js";
import { InProcessStore } from "../../src/exec/store.js";
import { toEnvelope } from "../../src/plugins/drain.js";
import { RotorError } from "../../src/errors.js";
import type { RotorDocument, StepResult } from "../../src/types.js";

const doc = (): RotorDocument =>
  ({
    apiVersion: "rotor.glyphh.ai/v0.1",
    kind: "Rotor",
    metadata: { name: "e", version: "0.1.0" },
    spec: { entry: "s", steps: [{ id: "s", type: "transform", in: {}, out: {}, next: "end" }] },
  }) as unknown as RotorDocument;

describe("a handler-thrown RotorError keeps its code on the tape + enriches the drain", () => {
  it("records the specific taxonomy code and only {name, cause}", async () => {
    const store = new InProcessStore();
    const r = await execute(doc(), {}, buildBasicPlugins({ store }), {
      handlers: {
        transform: {
          type: "transform",
          execute: async (): Promise<StepResult> => {
            throw new RotorError("E_TOOL", "downstream 500", { context: { tool: "query" } });
          },
        },
      },
    });
    expect(r.status).toBe("failed");
    const rec = r.history.find((h) => h.step_id === "s")!;
    expect(rec.error).toEqual({ name: "E_TOOL", cause: "downstream 500" }); // deterministic projection

    // The drain envelope is enriched from the catalog (telemetry).
    const env = toEnvelope(rec);
    expect(env.error).toMatchObject({
      name: "E_TOOL",
      category: "transport",
      retryable: true,
      severity: "error",
    });
    expect(env.error?.remediation).toBeTruthy();
  });

  it("normalizes a raw throw to E_HANDLER", async () => {
    const store = new InProcessStore();
    const r = await execute(doc(), {}, buildBasicPlugins({ store }), {
      handlers: {
        transform: {
          type: "transform",
          execute: async (): Promise<StepResult> => {
            throw new Error("kaboom");
          },
        },
      },
    });
    const rec = r.history.find((h) => h.step_id === "s")!;
    expect(rec.error?.name).toBe("E_HANDLER");
    expect(toEnvelope(rec).error?.category).toBe("internal");
  });
});
