/**
 * Runtime wiring + readiness tests (BUILD_PLAN.md Phase 1). Proves the manifest
 * reflects the REAL basic-tier plugins (not planned stubs), that readiness is
 * truthful (all-ready ⇒ ready; any down seam ⇒ not-ready), and that a rotor's
 * capability needs reconcile against the manifest.
 */

import { describe, it, expect } from "vitest";

import { Runtime, requiredCapabilities } from "../../src/runtime/runtime.js";
import { computeReadiness } from "../../src/server.js";
import { CAPABILITY_NAMES, type CapabilityStatus } from "../../src/runtime/registry.js";
import { manifestOf } from "../harness/caps.js";
import { loadFixture } from "../harness/fixtures.js";

describe("Runtime manifest", () => {
  const rt = new Runtime();

  it("advertises every seam as ready and basic-tier (real plugins, not stubs)", () => {
    const manifest = rt.status();
    expect(Object.keys(manifest).sort()).toEqual([...CAPABILITY_NAMES].sort());
    for (const [name, st] of Object.entries(manifest)) {
      expect(st.ready, name).toBe(true);
      expect(st.tier, name).toBe("basic");
      // A wired seam does not describe itself as "planned".
      expect(st.detail.toLowerCase(), name).not.toContain("planned");
    }
  });

  it("manifest matches the plugin bundle's own status() (advertised == real)", () => {
    expect(rt.status()).toEqual(manifestOf(rt.plugins));
  });
});

describe("computeReadiness", () => {
  it("is ready only when every seam is ready", () => {
    const rt = new Runtime();
    const r = computeReadiness(rt.status());
    expect(r.ready).toBe(true);
    expect(Object.keys(r.capabilities).sort()).toEqual([...CAPABILITY_NAMES].sort());
  });

  it("flips to not-ready when any required seam is down", () => {
    const manifest: Record<string, CapabilityStatus> = {};
    for (const name of CAPABILITY_NAMES) manifest[name] = { ready: true, detail: "ok", tier: "basic" };
    manifest.memory = { ready: false, detail: "stator unreachable", tier: "basic" };
    const r = computeReadiness(manifest);
    expect(r.ready).toBe(false);
    expect(r.capabilities.memory.ready).toBe(false);
  });

  it("is not-ready with an empty manifest", () => {
    expect(computeReadiness({}).ready).toBe(false);
  });
});

describe("capability reconciliation", () => {
  it("derives the seams a rotor's steps require", () => {
    const base = loadFixture("rotors/base.rotor.yaml");
    const required = requiredCapabilities(base);
    // The base rotor uses plan (models), tool (connections), gate/assert
    // (grounding), escalate (models).
    expect(required).toContain("models");
    expect(required).toContain("connections");
    expect(required).toContain("grounding");
  });

  it("reports satisfied against a fully-ready basic runtime", () => {
    const rt = new Runtime();
    const recon = rt.reconcile(loadFixture("rotors/base.rotor.yaml"));
    expect(recon.satisfied).toBe(true);
    expect(recon.unmet).toEqual([]);
  });
});
