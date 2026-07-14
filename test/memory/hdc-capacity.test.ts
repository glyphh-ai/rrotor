/**
 * HDC grounding capacity characterization (the "prove it or cut it" check). An
 * entity's cortex is a bundle of `role⊗filler` binds; superposition noise grows
 * with the number of pairs, so recall is perfect only while `vector_dim` is large
 * enough relative to facts-per-entity. This test locks the reliable envelope in CI
 * and asserts that degradation past it is graceful, not catastrophic.
 *
 * Measured (seed 42): dim 10 000 → 100% recall up to ~128 facts/entity, ~99% at
 * 200, decaying after; dim 32 000 → 100% to ~500. Rule of thumb: perfect while
 * facts_per_entity ≲ dim / 75.
 */

import { describe, it, expect } from "vitest";

import { HdcSpace } from "../../src/exec/hdc.js";

const FILLERS = Array.from({ length: 1000 }, (_, i) => `filler_${i}`);

/** Recall accuracy: encode one entity with `n` role→filler pairs, probe each role,
 *  and measure how often the associative cleanup returns the correct filler. */
function recallAccuracy(dim: number, n: number, seed = 42): number {
  const sp = new HdcSpace(dim, seed);
  const roleFillers: Record<string, string> = {};
  for (let i = 0; i < n; i++) roleFillers[`role_${i}`] = FILLERS[i];
  const { cortex } = sp.encodeRoleFillers(roleFillers);
  let correct = 0;
  for (let i = 0; i < n; i++) {
    const recalled = sp.bind(cortex, sp.roleSymbol(`role_${i}`));
    if (sp.cleanup(recalled, FILLERS.slice(0, n))[0].filler === FILLERS[i]) correct++;
  }
  return correct / n;
}

describe("HDC recall — reliable envelope (keep)", () => {
  it("is 100% at the default dim (10k) up to 100 facts/entity", () => {
    for (const n of [1, 10, 30, 60, 100]) {
      expect(recallAccuracy(10_000, n)).toBe(1);
    }
  });

  it("scales with vector_dim: a smaller space is perfect for fewer facts", () => {
    expect(recallAccuracy(2_000, 40)).toBe(1);
    expect(recallAccuracy(4_000, 80)).toBe(1);
  });
});

describe("HDC recall — graceful degradation past the envelope", () => {
  it("degrades smoothly (not catastrophically) when overloaded", () => {
    // Well past the reliable envelope, recall drops but stays well above chance
    // (chance ≈ 1/n), so grounding refuses on low margin rather than confidently
    // returning garbage.
    const overloaded = recallAccuracy(1_000, 80);
    expect(overloaded).toBeGreaterThan(0.5);
    expect(overloaded).toBeLessThan(1);
  });
});
