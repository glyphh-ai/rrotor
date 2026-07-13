/**
 * BasicGrounding — the HDC grounding engine (docs/runtime.md §3.1, SPEC.md §6.3,
 * §7.3, §15.4). It encodes a run's facts into hypervectors and grounds a claim by
 * associative recall + margin, rather than by exact string lookup.
 *
 * A space is fixed by `(vector_dim, encoder_seed)` → `space_id`; every symbol maps
 * to a deterministic bipolar hypervector, so the algebra is replay-safe (§6.2). An
 * entity's cortex is the bundle of its `role⊗filler` binds; probing unbinds by the
 * role and cleans up against the role's filler vocabulary, and `verify` grounds a
 * claim iff it is the winner with sufficient margin. `groundedFillers` is the
 * hard-gate mask — the set a `model` step may decode into (§6.3).
 *
 * The space invariant is enforced by identity: a cross-space bind is refused
 * (§15.4). The premium engine swaps in behind this same interface.
 */

import type { CapabilityStatus } from "../runtime/registry.js";
import { sha256 } from "../exec/util.js";
import { RotorError } from "../errors.js";
import { hdcSpace, type HdcSpace, type HyperVector } from "../exec/hdc.js";
import type { Stator } from "../exec/store.js";
import type {
  EncodeResult,
  GroundVerdict,
  GroundingPlugin,
  ProbeResult,
} from "./interfaces.js";

const norm = (s: unknown): string => String(s ?? "").trim().toLowerCase();
const DEFAULT_MARGIN = 0.05;

export class BasicGrounding implements GroundingPlugin {
  readonly name = "grounding";
  private bound: string | undefined;
  private dim = 10_000;
  private seed = 0;

  constructor(private readonly store: Stator) {}

  status(): CapabilityStatus {
    return { ready: true, detail: "HDC associative grounding + hard gate", tier: "basic" };
  }

  computeSpaceId(vectorDim: number, encoderSeed: number, rolesConfig: string): string {
    this.dim = vectorDim;
    this.seed = encoderSeed;
    const id = sha256(String(vectorDim), ":", String(encoderSeed), ":", rolesConfig);
    hdcSpace(id, vectorDim, encoderSeed); // prime the space + symbol cache
    return id;
  }

  assertSpace(spaceId: string): void {
    if (this.bound === undefined) {
      this.bound = spaceId;
      return;
    }
    if (this.bound !== spaceId) {
      throw new RotorError("E_SPACE_MISMATCH", `bound to ${this.bound}, asked for ${spaceId}`, {
        context: { bound: this.bound, asked: spaceId },
      });
    }
  }

  private space(spaceId?: string): HdcSpace {
    return hdcSpace(spaceId ?? this.bound, this.dim, this.seed);
  }

  encode(roleFillers: Record<string, string>, spaceId?: string): EncodeResult {
    const enc = this.space(spaceId).encodeRoleFillers(roleFillers);
    return { cortex: Array.from(enc.cortex), slots: enc.slots, dropped: enc.dropped };
  }

  /** The entity's cortex — the bundle of all its current `role⊗filler` binds. */
  private cortexFor(entity: string, sp: HdcSpace, spaceId?: string): HyperVector | undefined {
    const rows = this.store.query("lookup", { person: entity }, spaceId).rows;
    if (rows.length === 0) return undefined;
    const binds = rows.map((r) => sp.bind(sp.roleSymbol(String(r.role)), sp.symbol("filler:" + String(r.filler))));
    return sp.bundle(binds);
  }

  probe(entity: string, role: string, spaceId?: string): ProbeResult {
    const sp = this.space(spaceId);
    const cortex = this.cortexFor(entity, sp, spaceId);
    if (!cortex) return { filler: null, membership: 0, margin: 0, top: [] };
    // Candidate vocabulary for the role, across the space (distractors give margin).
    const candidates = this.store.query("top", { slot: role }, spaceId).rows.map((r) => String(r.filler));
    if (candidates.length === 0) return { filler: null, membership: 0, margin: 0, top: [] };
    const recalled = sp.bind(cortex, sp.roleSymbol(role));
    const ranked = sp.cleanup(recalled, candidates);
    const top1 = ranked[0];
    const margin = ranked.length > 1 ? top1.score - ranked[1].score : top1.score;
    return { filler: top1.filler, membership: top1.score, margin, top: ranked.map((r) => r.filler) };
  }

  verify(entity: string, role: string, filler: string, margin: number, spaceId?: string): GroundVerdict {
    const grounded = this.store.fillers(entity, role, spaceId);
    if (grounded.length === 0) return { grounded: false, membership: 0, margin: 0, top: [] };
    // The claim must reference a grounded filler (exact, or wrapped in a sentence).
    const cand = norm(filler);
    const matched = grounded.find((v) => {
      const n = norm(v);
      return n === cand || cand.includes(n) || n.includes(cand);
    });
    if (!matched) return { grounded: false, membership: 0, margin: 0, top: grounded };
    // HDC confidence: the associative recall must clear the margin threshold.
    const p = this.probe(entity, role, spaceId);
    const threshold = margin || DEFAULT_MARGIN;
    const confident = p.membership > 0 && p.margin >= threshold;
    return { grounded: confident, membership: p.membership, margin: p.margin, top: p.top };
  }

  groundedFillers(entity: string, role: string, spaceId?: string): string[] {
    return this.store.fillers(entity, role, spaceId);
  }
}
