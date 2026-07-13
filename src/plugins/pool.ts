/**
 * BasicPool — a single-process pool/affinity model (docs/runtime.md §3.7, SPEC
 * §17.2–§17.4). It tracks instance state (hot/warm/cold), routes requests by
 * affinity (`prefer`/`require` over tenant/conversation/entity keys), and warms
 * within a `maxHot` budget (§17.3).
 *
 * The load-bearing invariant: pooling is **determinism-neutral** (§17.6). Which
 * instance serves a request and how warm it is are OPERATIONAL telemetry — they
 * MUST NOT influence a run's transitions. The executor never consults this pool;
 * it is used only for routing/observability. An engine that ignores it entirely
 * produces the identical run, only slower.
 */

import type { CapabilityStatus } from "../runtime/registry.js";
import type { AffinityHint, InstanceState, PoolInstanceInfo, PoolPlugin } from "./interfaces.js";

interface Instance {
  id: string;
  state: InstanceState;
  keys: Set<string>;
}

export interface BasicPoolOptions {
  minHot?: number;
  maxHot?: number;
}

export class BasicPool implements PoolPlugin {
  readonly name = "pool";
  private readonly instances: Instance[] = [];
  private minHot: number;
  private maxHot: number;
  private counter = 0;

  constructor(opts: BasicPoolOptions = {}) {
    this.minHot = Math.max(1, opts.minHot ?? 1);
    this.maxHot = Math.max(this.minHot, opts.maxHot ?? 4);
  }

  status(): CapabilityStatus {
    return { ready: true, detail: `pool ${this.instances.length}/${this.maxHot} (hot/warm/cold); telemetry-only`, tier: "basic" };
  }

  /** Pre-warm up to `minHot` instances, never exceeding the `maxHot` budget. */
  provision(opts: { minHot?: number; maxHot?: number } = {}): void {
    if (opts.maxHot !== undefined) this.maxHot = Math.max(1, opts.maxHot);
    if (opts.minHot !== undefined) this.minHot = Math.max(1, opts.minHot);
    const target = Math.min(this.minHot, this.maxHot);
    while (this.instances.length < target) this.spawn("warm");
  }

  /**
   * Route to an instance honoring affinity (§17.4):
   *  - an instance already holding the affinity key wins (warm state reuse);
   *  - otherwise reuse a warm instance, or warm a new one within budget;
   *  - at budget, reuse the lowest-index instance (deterministic) — `require`
   *    degrades to a soft pin rather than exceeding the warming budget (§17.3).
   * The chosen instance becomes hot and records the key.
   */
  route(affinity?: AffinityHint): string {
    const key = affinity?.key;
    if (key) {
      const held = this.instances.find((i) => i.keys.has(key));
      if (held) {
        held.state = "hot";
        return held.id;
      }
    }
    const inst = this.pickOrWarm();
    if (key) inst.keys.add(key);
    inst.state = "hot";
    return inst.id;
  }

  instanceState(id?: string): InstanceState {
    if (id === undefined) return this.instances[0]?.state ?? "cold";
    return this.instances.find((i) => i.id === id)?.state ?? "cold";
  }

  snapshot(): PoolInstanceInfo[] {
    return this.instances.map((i) => ({ id: i.id, state: i.state, keys: [...i.keys].sort() }));
  }

  // ── internals ───────────────────────────────────────────────────────────────

  private pickOrWarm(): Instance {
    const warm = this.instances.find((i) => i.state === "warm");
    if (warm) return warm;
    if (this.instances.length < this.maxHot) return this.spawn("warm");
    // Budget reached — reuse the lowest-index instance rather than over-provision.
    return this.instances[0] ?? this.spawn("warm");
  }

  private spawn(state: InstanceState): Instance {
    const inst: Instance = { id: `local-${this.counter++}`, state, keys: new Set() };
    this.instances.push(inst);
    return inst;
  }
}
