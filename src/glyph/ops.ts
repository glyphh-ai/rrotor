/**
 * ops.ts — HDC vector operations. FAITHFUL PORT of the canonical
 * `glyphh/core/ops.py` (glyphh-ai/ada @ 041c0a20; see ../../glyphh/glyphh/
 * for the vendored reference the oracle suite runs this port against).
 *
 * Semantics, verbatim from the canon:
 *   - vectors are BIPOLAR int8 (+1 / -1)
 *   - bind      = elementwise product (self-inverse for bipolar)
 *   - bundle    = majority vote per dimension; ties (sum == 0) -> +1
 *   - cosine    = dot / dim, in [-1, 1]
 *   - hamming   = fraction of agreeing dimensions, in [0, 1]
 *   - generateSymbol(seed, key, dim) = deterministic atom:
 *       derived = int(sha256(`${seed}:${key}:${dim}`).hex[:8], 16)
 *       RandomState(derived).choice([-1, 1], size=dim)
 *     which reduces to bit 0 of successive MT19937 outputs seeded with
 *     init_by_array([derived]) — byte-identical to numpy (oracle-verified).
 */

import { createHash } from "node:crypto";
import { Mt19937 } from "./mt19937.js";

export type Bipolar = Int8Array;

export function bind(r: Bipolar, v: Bipolar): Bipolar {
  if (r.length !== v.length) {
    throw new Error(`Dimension mismatch: r has shape (${r.length},), v has shape (${v.length},)`);
  }
  const out = new Int8Array(r.length);
  for (let i = 0; i < r.length; i++) out[i] = (r[i]! * v[i]!) as -1 | 1;
  return out;
}

export function bundle(vectors: readonly Bipolar[]): Bipolar {
  if (!vectors.length) throw new Error("Cannot bundle empty vector list");
  const dim = vectors[0]!.length;
  for (let i = 0; i < vectors.length; i++) {
    if (vectors[i]!.length !== dim) {
      throw new Error(`Dimension mismatch: vector 0 has ${dim} dimensions, vector ${i} has ${vectors[i]!.length}`);
    }
  }
  const sums = new Int32Array(dim);
  for (const v of vectors) for (let i = 0; i < dim; i++) sums[i] += v[i]!;
  const out = new Int8Array(dim);
  for (let i = 0; i < dim; i++) out[i] = sums[i]! >= 0 ? 1 : -1;
  return out;
}

export function cosineSimilarity(a: Bipolar, b: Bipolar): number {
  if (a.length !== b.length) {
    throw new Error(`Dimension mismatch: v1 has shape (${a.length},), v2 has shape (${b.length},)`);
  }
  let dot = 0;
  for (let i = 0; i < a.length; i++) dot += a[i]! * b[i]!;
  return dot / a.length;
}

export function hammingSimilarity(a: Bipolar, b: Bipolar): number {
  if (a.length !== b.length) {
    throw new Error(`Dimension mismatch: v1 has shape (${a.length},), v2 has shape (${b.length},)`);
  }
  let agree = 0;
  for (let i = 0; i < a.length; i++) if (a[i] === b[i]) agree++;
  return agree / a.length;
}

export function generateSymbol(seed: number, key: string, dimension: number): Bipolar {
  const digest = createHash("sha256").update(`${seed}:${key}:${dimension}`, "utf8").digest("hex");
  const derived = parseInt(digest.slice(0, 8), 16);
  const mt = new Mt19937(derived);
  const out = new Int8Array(dimension);
  for (let i = 0; i < dimension; i++) out[i] = (mt.next32() & 1) === 1 ? 1 : -1;
  return out;
}
