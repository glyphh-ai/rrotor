/**
 * Runtime — the object graph a REPL session, the HTTP server, and the executor
 * hold.
 *
 * It constructs the BASIC-tier plugin bundle and registers each seam's REAL
 * `status()` in the capability registry, so the manifest the REPL and `/readyz`
 * read reflects what the runtime can actually do — not a roadmap of stubs. A
 * rotor's capability needs are reconciled against that manifest at load time
 * (docs/runtime.md §3.8): satisfied seams run; an unmet seam is surfaced so the
 * caller sees why a step would clean-refuse.
 */

import {
  CapabilityRegistry,
  CAPABILITY_NAMES,
  type CapabilityName,
  type CapabilityStatus,
} from "./registry.js";
import { buildBasicPlugins, type BuildBasicPluginsOptions } from "../plugins/index.js";
import type { Plugins } from "../plugins/interfaces.js";
import type { RotorDocument, StepType } from "../types.js";

/** Which capability seams each step type exercises. Used to reconcile a rotor's
 *  needs against the advertised manifest (docs/runtime.md §3.8). A step type
 *  absent from this map needs no seam (pure control/compose). */
const STEP_CAPABILITIES: Partial<Record<StepType, CapabilityName[]>> = {
  model: ["models"],
  plan: ["models"],
  "retrieve.sql": ["memory"],
  "retrieve.kb": ["memory", "grounding"],
  "hdc.map": ["grounding"],
  write: ["memory"],
  gate: ["grounding"],
  assert: ["grounding"],
  tool: ["connections"],
  escalate: ["models"],
};

export interface Reconciliation {
  /** Distinct seams the document's steps require. */
  required: CapabilityName[];
  /** Required seams whose advertised status is not ready. */
  unmet: CapabilityName[];
  satisfied: boolean;
}

export class Runtime {
  readonly registry = new CapabilityRegistry();
  readonly plugins: Plugins;

  constructor(opts: BuildBasicPluginsOptions = {}) {
    this.plugins = buildBasicPlugins(opts);
    // Register the real seam statuses — the manifest tells the truth.
    for (const name of CAPABILITY_NAMES) {
      const seam = this.plugins[name as keyof Plugins] as { status(): CapabilityStatus };
      this.registry.register({ name, status: () => seam.status() });
    }
  }

  status(): Record<string, CapabilityStatus> {
    return this.registry.manifest();
  }

  /** Reconcile a rotor's capability needs against the advertised manifest. */
  reconcile(doc: RotorDocument): Reconciliation {
    const manifest = this.status();
    const required = requiredCapabilities(doc);
    const unmet = required.filter((name) => !manifest[name]?.ready);
    return { required, unmet, satisfied: unmet.length === 0 };
  }
}

/** The distinct capability seams a document's steps exercise, sorted. */
export function requiredCapabilities(doc: RotorDocument): CapabilityName[] {
  const needed = new Set<CapabilityName>();
  for (const step of doc.spec.steps) {
    for (const cap of STEP_CAPABILITIES[step.type] ?? []) needed.add(cap);
  }
  return [...needed].sort();
}
