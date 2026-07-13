/**
 * BasicGrounding — the open, pure-numeric `ExactMatchGrounding` (docs/runtime.md
 * §3.1). It is honest but not associative: it does exact lookup over the fact
 * store's `(entity, role) → filler` triples and refuses on a miss. There is NO
 * HDC algebra and NO hard logit gate here — the reference runtime abstracts
 * `hdc.map` and does not practice the patent. The premium swap-in is the real
 * HDC engine behind this same interface.
 *
 * The space invariant is still enforced by identity: `space_id = sha256(dim,
 * seed, roles)`; a cross-space bind is refused (§15.4).
 */

import type { CapabilityStatus } from "../runtime/registry.js";
import { sha256 } from "../exec/util.js";
import type { Stator } from "../exec/store.js";
import type {
  EncodeResult,
  GroundVerdict,
  GroundingPlugin,
  ProbeResult,
} from "./interfaces.js";

const norm = (s: unknown): string => String(s ?? "").trim().toLowerCase();

export class BasicGrounding implements GroundingPlugin {
  readonly name = "grounding";
  private bound: string | undefined;

  constructor(private readonly store: Stator) {}

  status(): CapabilityStatus {
    return { ready: true, detail: "exact-match; no hard gate", tier: "basic" };
  }

  computeSpaceId(vectorDim: number, encoderSeed: number, rolesConfig: string): string {
    return sha256(String(vectorDim), ":", String(encoderSeed), ":", rolesConfig);
  }

  assertSpace(spaceId: string): void {
    if (this.bound === undefined) {
      this.bound = spaceId;
      return;
    }
    if (this.bound !== spaceId) {
      throw new Error(`E_SPACE_MISMATCH: bound to ${this.bound}, asked for ${spaceId}`);
    }
  }

  encode(roleFillers: Record<string, string>, _spaceId?: string): EncodeResult {
    const slots: Array<[string, string]> = [];
    const dropped: Array<[string, string]> = [];
    for (const role of Object.keys(roleFillers).sort()) {
      const filler = roleFillers[role];
      if (filler === undefined || filler === null || String(filler).trim() === "") {
        dropped.push([role, String(filler)]);
      } else {
        slots.push([role, String(filler)]);
      }
    }
    // Basic tier emits an empty cortex — it does not compute hypervectors.
    return { cortex: [], slots, dropped };
  }

  probe(entity: string, role: string, spaceId?: string): ProbeResult {
    const f = this.store.lookupFact(entity, role, spaceId);
    if (!f) return { filler: null, membership: 0, margin: 0, top: [] };
    return { filler: f.filler, membership: 1, margin: 1, top: [f.filler] };
  }

  verify(entity: string, role: string, filler: string, _margin: number, spaceId?: string): GroundVerdict {
    const fillers = this.store.fillers(entity, role, spaceId);
    if (fillers.length === 0) return { grounded: false, membership: 0, margin: 0, top: [] };
    const cand = norm(filler);
    // Exact match, or the stored filler appears as a token in the proposal
    // (the model may wrap the grounded value in a sentence).
    const hit = fillers.find((v) => {
      const n = norm(v);
      return n === cand || cand.includes(n) || n.includes(cand);
    });
    if (hit) return { grounded: true, membership: 1, margin: 1, top: fillers };
    return { grounded: false, membership: 0, margin: 0, top: fillers };
  }

  groundedFillers(entity: string, role: string, spaceId?: string): string[] {
    return this.store.fillers(entity, role, spaceId);
  }
}
