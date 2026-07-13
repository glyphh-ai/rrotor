/**
 * A deterministic local text embedding — the basic-tier short-term-memory /
 * semantic-recall lane (SPEC.md §7.7), chosen for the open runtime because it is
 * **replay-safe by construction**: pure hashing of word tokens + character
 * trigrams into a fixed-dimension vector, no model call and no RNG, so the same
 * text always embeds to the same vector. A neural embedding is a premium lane that
 * must be checkpointed at the embedding boundary to stay replay-safe.
 *
 * The vector is the L2-normalized signed hash of the token bag (the "hashing
 * trick"), so cosine similarity is a meaningful, order-independent lexical/subword
 * signal — a real upgrade over raw token-overlap.
 */

const DIM = 256;

function hash32(s: string): number {
  let h = 2166136261 >>> 0;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/** Word tokens plus character trigrams — subword features catch morphology and
 *  typos that whole-word overlap misses. */
function features(text: string): string[] {
  const lower = text.toLowerCase();
  const words = lower.split(/[^a-z0-9]+/).filter(Boolean);
  const feats: string[] = [...words.map((w) => "w:" + w)];
  for (const w of words) {
    const padded = `#${w}#`;
    for (let i = 0; i + 3 <= padded.length; i++) feats.push("t:" + padded.slice(i, i + 3));
  }
  return feats;
}

/** Embed text into a unit-length vector of length {@link DIM}. Deterministic. */
export function embed(text: string, dim = DIM): number[] {
  const v = new Array<number>(dim).fill(0);
  for (const f of features(text)) {
    const h = hash32(f);
    const bucket = h % dim;
    // A second hash bit gives the sign, so collisions cancel rather than pile up.
    const sign = (h & 0x80000000) !== 0 ? -1 : 1;
    v[bucket] += sign;
  }
  let norm = 0;
  for (const x of v) norm += x * x;
  norm = Math.sqrt(norm);
  if (norm === 0) return v;
  for (let i = 0; i < dim; i++) v[i] /= norm;
  return v;
}

/** Cosine similarity of two equal-length vectors. */
export function cosine(a: number[], b: number[]): number {
  const n = Math.min(a.length, b.length);
  let dot = 0;
  for (let i = 0; i < n; i++) dot += a[i] * b[i];
  return dot;
}

/** Convenience: cosine similarity of two raw texts under {@link embed}. */
export function textSimilarity(a: string, b: string): number {
  return cosine(embed(a), embed(b));
}
