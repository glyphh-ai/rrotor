/**
 * AttentionMeter unit tests (BUILD_PLAN.md Phase 4). The meter is the deterministic
 * core of §10 budget enforcement — only revolutions/tokens/cost (recomputable from
 * recorded state), never wall-clock.
 */

import { describe, it, expect } from "vitest";

import { AttentionMeter } from "../../src/exec/budget.js";

describe("AttentionMeter", () => {
  it("never exhausts without a budget", () => {
    const m = new AttentionMeter(undefined);
    m.revolution();
    m.record({ input: 999, output: 999, cost: 999 });
    expect(m.exhausted()).toBeUndefined();
  });

  it("exhausts on revolutions (>=) with the configured on_exhausted", () => {
    const m = new AttentionMeter({ revolutions: 2, on_exhausted: "escalate" });
    m.revolution();
    expect(m.exhausted()).toBeUndefined();
    m.revolution();
    expect(m.exhausted()).toMatchObject({ reason: "revolutions", on: "escalate", revolutions: 2 });
  });

  it("exhausts on tokens (input + output, strict >)", () => {
    const m = new AttentionMeter({ tokens: 10 });
    m.record({ input: 6, output: 4 }); // 10 — at the bound, not over
    expect(m.exhausted()).toBeUndefined();
    m.record({ input: 1 }); // 11 — over
    expect(m.exhausted()).toMatchObject({ reason: "tokens", on: "stop" });
  });

  it("exhausts on cost", () => {
    const m = new AttentionMeter({ cost: 1 });
    m.record({ cost: 1.5 });
    expect(m.exhausted()).toMatchObject({ reason: "cost", cost: 1.5 });
  });

  it("checks revolutions before tokens before cost", () => {
    const m = new AttentionMeter({ revolutions: 1, tokens: 1, cost: 1 });
    m.revolution();
    m.record({ input: 100, cost: 100 });
    expect(m.exhausted()?.reason).toBe("revolutions");
  });
});
