/**
 * Capability-manifest tests. Pins the shape the basic-tier bundle advertises so a
 * later phase that adds a seam (e.g. the `drain` capability in Phase 3) or
 * changes a tier updates this deliberately.
 */

import { describe, it, expect } from "vitest";

import { buildBasicPlugins } from "../../src/plugins/index.js";
import { CAPABILITY_NAMES } from "../../src/runtime/registry.js";
import { manifestOf, seamNames, tierMap } from "../harness/caps.js";

describe("basic-tier capability manifest", () => {
  const plugins = buildBasicPlugins();
  const manifest = manifestOf(plugins);

  it("advertises exactly the declared capability seams", () => {
    expect(seamNames(manifest)).toEqual([...CAPABILITY_NAMES].sort());
  });

  it("every seam reports a well-formed status", () => {
    for (const [name, status] of Object.entries(manifest)) {
      expect(typeof status.ready, name).toBe("boolean");
      expect(typeof status.detail, name).toBe("string");
      expect(["basic", "premium", "none"], name).toContain(status.tier);
    }
  });

  it("all basic seams are ready and basic-tier", () => {
    for (const [name, tier] of Object.entries(tierMap(manifest))) {
      expect(tier, name).toBe("basic");
    }
    for (const [name, status] of Object.entries(manifest)) {
      expect(status.ready, name).toBe(true);
    }
  });
});
