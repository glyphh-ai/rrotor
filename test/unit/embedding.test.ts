/**
 * Deterministic embedding unit tests (BUILD_PLAN.md Phase 6). Replay-safe by
 * construction: same text → same vector; cosine is a meaningful subword/lexical
 * signal.
 */

import { describe, it, expect } from "vitest";

import { embed, textSimilarity } from "../../src/exec/embedding.js";

describe("embed / cosine", () => {
  it("is deterministic and unit-length", () => {
    expect(embed("hello world")).toEqual(embed("hello world"));
    const v = embed("the quick brown fox");
    const norm = Math.sqrt(v.reduce((s, x) => s + x * x, 0));
    expect(norm).toBeCloseTo(1, 6);
  });

  it("self-similarity is 1", () => {
    expect(textSimilarity("openrotor runtime", "openrotor runtime")).toBeCloseTo(1, 6);
  });

  it("ranks related text above unrelated text", () => {
    const related = textSimilarity("the cat sat on the mat", "a cat is sitting");
    const unrelated = textSimilarity("the cat sat on the mat", "quarterly revenue projections");
    expect(related).toBeGreaterThan(unrelated);
  });

  it("empty text embeds to a zero vector (cosine 0)", () => {
    expect(textSimilarity("", "anything")).toBe(0);
  });
});
