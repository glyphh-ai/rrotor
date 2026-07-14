/**
 * Capability-manifest assertion helper (BUILD_PLAN.md Phase 0).
 *
 * Every plugin seam reports a `status(): { ready, detail, tier }`. This helper
 * projects a plugin bundle into a `{ name → status }` manifest so tests can pin
 * the advertised tier map — and catch regressions where a seam silently stops
 * reporting or a wired plugin diverges from what the runtime advertises.
 */

import { CAPABILITY_NAMES, type CapabilityStatus } from "../../src/runtime/registry.js";
import type { Plugins } from "../../src/plugins/interfaces.js";

export type Manifest = Record<string, CapabilityStatus>;

/** Build the manifest directly from a plugin bundle (the reality on the wire). */
export function manifestOf(plugins: Plugins): Manifest {
  const out: Manifest = {};
  for (const name of CAPABILITY_NAMES) {
    const seam = plugins[name as keyof Plugins] as { status(): CapabilityStatus };
    out[name] = seam.status();
  }
  return out;
}

/** The set of seam names present in a manifest. */
export function seamNames(manifest: Manifest): string[] {
  return Object.keys(manifest).sort();
}

/** Map each seam to its advertised tier — the coarse shape a test pins. */
export function tierMap(manifest: Manifest): Record<string, CapabilityStatus["tier"]> {
  const out: Record<string, CapabilityStatus["tier"]> = {};
  for (const [name, s] of Object.entries(manifest)) out[name] = s.tier;
  return out;
}
