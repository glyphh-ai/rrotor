/**
 * The HDC (hyperdimensional computing) engine — the vector-symbolic algebra the
 * grounding law runs on (SPEC.md §6.3, §7.3, §15.4).
 *
 * A **space** is fixed by `(vector_dim, encoder_seed)`: every symbol deterministically
 * maps to the SAME bipolar hypervector (±1 per dimension) within a space, generated
 * by a seeded PRNG over a hash of `(seed, symbol)`. Because generation is pure
 * arithmetic (no crypto, no RNG state outside the seed), two runs in the same space
 * produce byte-identical vectors — the algebra is replay-safe (§6.2).
 *
 * The operations are the standard VSA trio:
 *  - **bind** (⊗, elementwise product): associates a role with a filler; it is its
 *    own inverse for bipolar vectors, so binding the cortex by a role UNBINDS it.
 *  - **bundle** (⊕, majority sign of the sum): superposes many bound pairs into one
 *    cortex vector that approximately contains each.
 *  - **permute** (cyclic shift): encodes position/sequence.
 * Cleanup is nearest-neighbour by **cosine** (= dot / dim for bipolar vectors).
 */

export type HyperVector = Int8Array;

/** mulberry32 — a tiny deterministic PRNG (pure arithmetic on a 32-bit seed). */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** A deterministic 32-bit string hash (FNV-1a), mixed with a numeric seed. */
function hash32(seed: number, s: string): number {
  let h = (2166136261 ^ seed) >>> 0;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

export class HdcSpace {
  readonly dim: number;
  readonly seed: number;
  private readonly cache = new Map<string, HyperVector>();

  constructor(dim = 10_000, seed = 0) {
    // Bound the dimension so a pathological `vector_dim` can't exhaust memory.
    this.dim = Math.max(64, Math.min(dim, 65_536));
    this.seed = seed >>> 0;
  }

  /** The deterministic bipolar hypervector for a symbol (cached). */
  symbol(s: string): HyperVector {
    const cached = this.cache.get(s);
    if (cached) return cached;
    const rnd = mulberry32(hash32(this.seed, s));
    const v = new Int8Array(this.dim);
    for (let i = 0; i < this.dim; i++) v[i] = rnd() < 0.5 ? -1 : 1;
    this.cache.set(s, v);
    return v;
  }

  /** bind (⊗): elementwise product — its own inverse for bipolar vectors. */
  bind(a: HyperVector, b: HyperVector): HyperVector {
    const out = new Int8Array(this.dim);
    for (let i = 0; i < this.dim; i++) out[i] = (a[i] * b[i]) as -1 | 0 | 1;
    return out;
  }

  /** bundle (⊕): majority sign of the elementwise sum (ties → +1). */
  bundle(vectors: HyperVector[]): HyperVector {
    const acc = new Int32Array(this.dim);
    for (const v of vectors) for (let i = 0; i < this.dim; i++) acc[i] += v[i];
    const out = new Int8Array(this.dim);
    for (let i = 0; i < this.dim; i++) out[i] = acc[i] >= 0 ? 1 : -1;
    return out;
  }

  /** permute: cyclic left-shift by `n` (encodes position). */
  permute(v: HyperVector, n = 1): HyperVector {
    const d = this.dim;
    const shift = ((n % d) + d) % d;
    const out = new Int8Array(d);
    for (let i = 0; i < d; i++) out[i] = v[(i + shift) % d];
    return out;
  }

  /** cosine similarity in [-1, 1]; for bipolar vectors this is dot / dim. */
  cosine(a: HyperVector, b: HyperVector): number {
    let dot = 0;
    for (let i = 0; i < this.dim; i++) dot += a[i] * b[i];
    return dot / this.dim;
  }

  /** Encode a set of role→filler pairs into one cortex vector: bundle of the
   *  role⊗filler binds. Empty/blank fillers are dropped. */
  encodeRoleFillers(roleFillers: Record<string, string>): {
    cortex: HyperVector;
    slots: Array<[string, string]>;
    dropped: Array<[string, string]>;
  } {
    const binds: HyperVector[] = [];
    const slots: Array<[string, string]> = [];
    const dropped: Array<[string, string]> = [];
    for (const role of Object.keys(roleFillers).sort()) {
      const filler = roleFillers[role];
      if (filler === undefined || filler === null || String(filler).trim() === "") {
        dropped.push([role, String(filler)]);
        continue;
      }
      binds.push(this.bind(this.symbol("role:" + role), this.symbol("filler:" + String(filler))));
      slots.push([role, String(filler)]);
    }
    const cortex = binds.length > 0 ? this.bundle(binds) : new Int8Array(this.dim);
    return { cortex, slots, dropped };
  }

  /**
   * Clean up an unbound query vector against a candidate vocabulary: rank the
   * candidates by cosine to `query`, returning the sorted list with scores. The
   * margin (top1 − top2) is how the grounding gate decides confidence.
   */
  cleanup(query: HyperVector, candidates: string[]): Array<{ filler: string; score: number }> {
    return candidates
      .map((filler) => ({ filler, score: this.cosine(query, this.symbol("filler:" + filler)) }))
      .sort((a, b) => b.score - a.score || (a.filler < b.filler ? -1 : a.filler > b.filler ? 1 : 0));
  }

  roleSymbol(role: string): HyperVector {
    return this.symbol("role:" + role);
  }
}

/** Space cache keyed by `space_id` so a run reuses one space (and its symbol
 *  cache) across steps. Pure given `(dim, seed)`. */
const SPACES = new Map<string, HdcSpace>();

export function hdcSpace(spaceId: string | undefined, dim: number, seed: number): HdcSpace {
  const key = spaceId ?? `anon:${dim}:${seed}`;
  let sp = SPACES.get(key);
  if (!sp) {
    sp = new HdcSpace(dim, seed);
    SPACES.set(key, sp);
  }
  return sp;
}
