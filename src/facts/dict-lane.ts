/**
 * facts/dict-lane.ts — the SECONDARY vector over every inbound word
 * (docs/dict-vector.md). Three vocabularies, one algebra, literals as
 * payload only:
 *
 *   atoms   — every word, minted on demand (generateSymbol; zero storage)
 *   primes  — the NSM set, universal semantics
 *   labels  — taxonomy concepts (curated glosses = the synonymy bridge)
 *
 * A fact's dict vector = IDF-weighted word atoms over ALL its values
 * (schema slots AND loose-capture extras) ⊕ its primes ⊕ its applies_to
 * labels. The exchange encodes the same way each turn. Selection is one
 * cosine sweep — deterministic, model-free, sub-millisecond.
 *
 * This lane is DERIVED: float accumulation (weights must survive — bipolar
 * majority would erase them), its own dimension and seed family, rebuilt
 * from concept JSON at hydration. The canonical glyph cortex and its
 * space_id are untouched.
 *
 * Determinism note: IDF comes from the org lexicon SNAPSHOT taken at
 * hydration — the in-memory index IS the epoch. Same ledger + same
 * snapshot → same block, replayable; the epoch stamp rides the index.
 */

import { generateSymbol } from "../glyph/ops.js";
import { decompose } from "../exec/glyph/primes.js";
import { TAXONOMY } from "./taxonomy.js";

export const DICT_DIM = 2048;
const DICT_SEED = 7; // distinct atom family — never the glyph space's seed

/** Weight of a prime atom (primes are already a rare namespace). */
const PRIME_WEIGHT = 1.0;
/** Weight of a taxonomy-label atom (strong aboutness — one label ≈ several words). */
const LABEL_WEIGHT = 2.0;

// ── atoms ──────────────────────────────────────────────────────────────────

const atomCache = new Map<string, Int8Array>();

/** Deterministic atom for any key, cached. Namespaced: w:/p:/l: so a word
 *  that happens to equal a label slug can never collide with it. */
export function dictAtom(key: string): Int8Array {
  let a = atomCache.get(key);
  if (!a) {
    a = generateSymbol(DICT_SEED, key, DICT_DIM);
    atomCache.set(key, a);
  }
  return a;
}

// ── tokenization ───────────────────────────────────────────────────────────

/** Lowercase, unicode-fold, split on non-alphanumerics. No stopword list —
 *  IDF is the stopword mechanism (rules age better than lists). */
export function tokenize(text: string): string[] {
  return text
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .split(/[^a-z0-9]+/)
    .filter((w) => w.length > 1);
}

// ── IDF ────────────────────────────────────────────────────────────────────

/** Monotone-decreasing rarity weight from a lexicon count. Gentle at low
 *  counts (a word seen once — often BY the fact that carries it — must not
 *  self-damp its own selection), steep on genuinely common words. Unseen →
 *  1.0; count 4 → ~0.63; count 1000 → ~0.13. Deterministic per snapshot. */
export function idfWeight(count: number): number {
  return 1 / Math.log2(2 + Math.max(0, count) / 4);
}

export type Lexicon = ReadonlyMap<string, number>;

// ── vectors ────────────────────────────────────────────────────────────────

function accumulate(v: Float32Array, atom: Int8Array, weight: number): void {
  for (let i = 0; i < v.length; i++) v[i] += atom[i]! * weight;
}

function l2normalize(v: Float32Array): Float32Array {
  let norm = 0;
  for (const x of v) norm += x * x;
  norm = Math.sqrt(norm);
  if (norm > 0) for (let i = 0; i < v.length; i++) v[i]! /= norm;
  return v;
}

/** Weighted bundle over words (IDF), primes and labels. Duplicate words
 *  accumulate once per occurrence — repetition IS emphasis, IDF caps abuse. */
export function dictVector(parts: { words: string[]; primes?: string[]; labels?: string[] }, lexicon: Lexicon): Float32Array {
  const v = new Float32Array(DICT_DIM);
  for (const w of parts.words) accumulate(v, dictAtom(`w:${w}`), idfWeight(lexicon.get(w) ?? 0));
  for (const p of parts.primes ?? []) accumulate(v, dictAtom(`p:${p}`), PRIME_WEIGHT);
  for (const l of parts.labels ?? []) accumulate(v, dictAtom(`l:${l}`), LABEL_WEIGHT);
  return l2normalize(v);
}

export function dictCosine(a: Float32Array, b: Float32Array): number {
  let dot = 0;
  for (let i = 0; i < a.length; i++) dot += a[i]! * b[i]!;
  return dot; // both unit-length
}

// ── the fact side ──────────────────────────────────────────────────────────

/** Every value word in a stored concept — schema slots and preserved extras
 *  alike (loose capture feeds the lane; that is the point). */
export function conceptWords(name: string, facts: Record<string, Record<string, string>>): string[] {
  const words = tokenize(name);
  for (const roles of Object.values(facts)) for (const value of Object.values(roles)) words.push(...tokenize(value));
  return words;
}

/** The rule layer convention (docs/dict-vector.md): a directive is a fact
 *  whose concept carries `rule: {action, object?, applies_to?, condition?}` —
 *  off-schema by design, preserved by loose capture, recognized here. */
export interface DirectiveShape {
  action: string;
  object?: string;
  appliesTo: string[];
}

export function directiveOf(facts: Record<string, Record<string, string>>): DirectiveShape | null {
  const rule = facts.rule;
  if (!rule || !rule.action) return null;
  const appliesTo = (rule.applies_to ?? "").split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
  return { action: rule.action, ...(rule.object !== undefined ? { object: rule.object } : {}), appliesTo };
}

export function factDictVector(
  name: string,
  facts: Record<string, Record<string, string>>,
  primes: string[],
  lexicon: Lexicon,
): Float32Array {
  const labels = directiveOf(facts)?.appliesTo ?? [];
  return dictVector({ words: conceptWords(name, facts), primes, labels }, lexicon);
}

// ── the exchange side ──────────────────────────────────────────────────────

/** Gloss vectors for the taxonomy — words only (a gloss IS its words), built
 *  against the same lexicon snapshot as everything else in the epoch. */
export function glossVectors(lexicon: Lexicon): Map<string, Float32Array> {
  const out = new Map<string, Float32Array>();
  for (const t of TAXONOMY) out.set(t.label, dictVector({ words: tokenize(t.gloss) }, lexicon));
  return out;
}

/** Classify an exchange into taxonomy labels — top-k cosine over gloss
 *  vectors, permissive gate (a missed label costs correctness; a spurious
 *  one costs a block slot — the cheap side). Deterministic. */
export function classifyExchange(
  exchangeVec: Float32Array,
  glosses: ReadonlyMap<string, Float32Array>,
  topK = 3,
  gate = 0.12,
): string[] {
  const scored: Array<{ label: string; cos: number }> = [];
  for (const [label, vec] of glosses) {
    const cos = dictCosine(exchangeVec, vec);
    if (cos >= gate) scored.push({ label, cos });
  }
  scored.sort((a, b) => b.cos - a.cos);
  return scored.slice(0, topK).map((s) => s.label);
}

const EXCHANGE_WORD_CAP = 300;

/** Words-only exchange vector — the one gloss/label comparisons use (primes
 *  ride the full vector for fact scoring but only DILUTE gloss cosines, since
 *  glosses are pure words). */
export function wordsVector(text: string, lexicon: Lexicon): Float32Array {
  return dictVector({ words: tokenize(text).slice(-EXCHANGE_WORD_CAP) }, lexicon);
}

/** Words-only exchange vector (labels are classified FROM this, then the
 *  caller folds them in via {@link dictVector} if needed — but selection
 *  compares label atoms on the fact side against classified labels directly,
 *  so the exchange vector stays words+primes). Tail-weighted: the newest
 *  words (the current prompt) count double. */
export function exchangeVector(exchangeText: string, lexicon: Lexicon): Float32Array {
  const words = tokenize(exchangeText).slice(-EXCHANGE_WORD_CAP);
  const half = Math.floor(words.length / 2);
  const weighted = [...words, ...words.slice(half)]; // tail twice ≈ recency ×2
  return dictVector({ words: weighted, primes: decompose(exchangeText) }, lexicon);
}
