/**
 * Runtime — the object graph a REPL session and (later) the executor hold.
 *
 * From-zero milestone: it wires the capability registry with BASIC-tier stubs so
 * `status` shows the roadmap. The real handlers land in the next build phases
 * (parser → executor + basic plugins → gateway → conformance), per the build
 * plan in docs/runtime.md §6.
 */

import {
  CapabilityRegistry,
  CAPABILITY_NAMES,
  type Capability,
  type CapabilityName,
} from "./registry.js";

/** The open BASIC implementation each seam will ship (docs/runtime.md §3). */
const BASIC_PLAN: Record<CapabilityName, string> = {
  grounding: "exact-match + verify-or-refuse",
  memory: "SQLite stator",
  models: "local OpenAI-compatible lane",
  connections: "loopback handler registry",
  gateway: "identity adapters",
  governance: "local ledger, deny-by-default",
  pool: "single-instance (cold-start)",
};

export class Runtime {
  readonly registry = new CapabilityRegistry();

  constructor() {
    for (const name of CAPABILITY_NAMES) {
      this.registry.register(planned(name, BASIC_PLAN[name]));
    }
  }

  status(): Record<string, { ready: boolean; detail: string; tier: string }> {
    return this.registry.manifest();
  }
}

/** A not-yet-built capability that advertises its planned basic implementation. */
function planned(name: CapabilityName, plan: string): Capability {
  return {
    name,
    status: () => ({ ready: false, detail: `${plan} — planned`, tier: "basic" as const }),
  };
}
