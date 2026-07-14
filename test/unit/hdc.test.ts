/**
 * HDC engine unit tests (BUILD_PLAN.md Phase 6). Proves the vector-symbolic algebra:
 * deterministic seeded symbols, near-orthogonality of distinct symbols, bind as its
 * own inverse, and an encode → unbind → cleanup round-trip that recovers the bound
 * filler with a clear margin.
 */

import { describe, it, expect } from "vitest";

import { HdcSpace } from "../../src/exec/hdc.js";

describe("HdcSpace", () => {
  it("maps a symbol to the same vector deterministically (seeded)", () => {
    const a = new HdcSpace(4096, 42);
    const b = new HdcSpace(4096, 42);
    expect(Array.from(a.symbol("x"))).toEqual(Array.from(b.symbol("x")));
  });

  it("gives distinct symbols near-orthogonal vectors", () => {
    const sp = new HdcSpace(10_000, 7);
    expect(Math.abs(sp.cosine(sp.symbol("morgan"), sp.symbol("paris")))).toBeLessThan(0.05);
    expect(sp.cosine(sp.symbol("x"), sp.symbol("x"))).toBeCloseTo(1, 6);
  });

  it("changes vectors with the seed (different spaces)", () => {
    const a = new HdcSpace(4096, 1);
    const b = new HdcSpace(4096, 2);
    expect(Math.abs(a.cosine(a.symbol("x"), b.symbol("x")))).toBeLessThan(0.05);
  });

  it("bind is its own inverse for bipolar vectors", () => {
    const sp = new HdcSpace(4096, 3);
    const role = sp.symbol("role");
    const filler = sp.symbol("filler");
    const bound = sp.bind(role, filler);
    // Unbinding by the role recovers the filler exactly.
    expect(Array.from(sp.bind(bound, role))).toEqual(Array.from(filler));
  });

  it("round-trips an encoded cortex: unbind by role recovers the filler", () => {
    const sp = new HdcSpace(10_000, 42);
    const { cortex } = sp.encodeRoleFillers({ "rel.spouse": "morgan", "rel.city": "london" });
    const recalled = sp.bind(cortex, sp.roleSymbol("rel.spouse"));
    const ranked = sp.cleanup(recalled, ["morgan", "london", "paris", "sam"]);
    expect(ranked[0].filler).toBe("morgan");
    const margin = ranked[0].score - ranked[1].score;
    expect(margin).toBeGreaterThan(0.2);
  });
});
