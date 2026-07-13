/**
 * §5.7 result-cache scope tests (BUILD_PLAN.md Phase 2). Proves the scope prefix
 * confines reuse to the right boundary and — per SPEC §5.7 — that `tenant`/`global`
 * keys always carry `space_id` so an entry can never bind across HDC spaces.
 */

import { describe, it, expect } from "vitest";

import { scopedCacheKey } from "../../src/exec/util.js";

const ids = (over: Partial<{ runId: string; definitionVersion: string; spaceId?: string }> = {}) => ({
  runId: "run-1",
  definitionVersion: "0.1.0",
  spaceId: "spaceA",
  ...over,
});

describe("scopedCacheKey", () => {
  it("run scope isolates per run_id", () => {
    const a = scopedCacheKey("k", "run", ids({ runId: "run-1" }));
    const b = scopedCacheKey("k", "run", ids({ runId: "run-2" }));
    expect(a).not.toBe(b);
  });

  it("rotor scope (the default) shares across runs of the same version", () => {
    const a = scopedCacheKey("k", "rotor", ids({ runId: "run-1" }));
    const b = scopedCacheKey("k", "rotor", ids({ runId: "run-2" }));
    expect(a).toBe(b);
    // Default scope == rotor.
    expect(scopedCacheKey("k", undefined, ids())).toBe(a);
  });

  it("tenant scope isolates by space_id (§5.7 — no cross-space bind)", () => {
    const a = scopedCacheKey("k", "tenant", ids({ spaceId: "spaceA" }));
    const b = scopedCacheKey("k", "tenant", ids({ spaceId: "spaceB" }));
    expect(a).not.toBe(b);
    expect(a).toContain("spaceA");
  });

  it("global scope also isolates by space_id (§5.7 MUST)", () => {
    const a = scopedCacheKey("k", "global", ids({ spaceId: "spaceA" }));
    const b = scopedCacheKey("k", "global", ids({ spaceId: "spaceB" }));
    expect(a).not.toBe(b);
    expect(a).toContain("spaceA");
  });

  it("tenant and global occupy distinct namespaces", () => {
    expect(scopedCacheKey("k", "tenant", ids())).not.toBe(scopedCacheKey("k", "global", ids()));
  });
});
