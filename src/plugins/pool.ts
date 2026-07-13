/**
 * BasicPool — the no-op / single-instance pool (docs/runtime.md §3.7). It cold-
 * starts every run, routes every request to the one local instance, and ignores
 * `spec.pool` / `spec.affinity` entirely. This is fully conformant at every
 * level because pooling/affinity are determinism-neutral (§17.6): an engine that
 * ignores them produces the identical run, only slower. Which instance served
 * and how warm it is are telemetry ONLY and MUST NOT influence a transition.
 */

import type { CapabilityStatus } from "../runtime/registry.js";
import type { InstanceState, PoolPlugin } from "./interfaces.js";

export class BasicPool implements PoolPlugin {
  readonly name = "pool";

  status(): CapabilityStatus {
    return { ready: true, detail: "single-instance (cold-start)", tier: "basic" };
  }

  provision(): void {
    // No-op: there is one local instance and nothing to warm.
  }

  route(): string {
    return "local-0";
  }

  instanceState(_id?: string): InstanceState {
    // The single local instance is always "hot" once the process is up.
    return "hot";
  }
}
