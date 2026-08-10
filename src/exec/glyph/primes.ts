/**
 * primes.ts — semantic primes, the decomposition ontology at the center of the
 * glyph. Ported from `glyphh_rotor/primes/primes.py` (the source-of-truth engine).
 *
 * Every idea is taken down to the primes it relates to (the NSM universal
 * primitives: KNOW, WANT, FEEL, DO, HAPPEN, WHERE, WHEN, GOOD, BAD, …). Primes are
 * stored on turns/events at write time; at recall the prompt's own primes select
 * and group what comes back — that grouping is the fact tree.
 *
 * Decomposition is heuristic-first: a deterministic keyword map, always available,
 * costs nothing. A local model may SHARPEN it (validated against the prime list —
 * the model may only CHOOSE primes, never invent them). Determinism-by-purity for
 * the heuristic path; the sharpened path is checkpointed by the caller.
 */

/** The NSM semantic primes, grouped as in the literature — a closed vocabulary. */
export const PRIMES: Record<string, string[]> = {
  substantives: ["I", "YOU", "SOMEONE", "SOMETHING", "PEOPLE", "BODY"],
  relational: ["KIND", "PART"],
  determiners: ["THIS", "SAME", "OTHER"],
  quantifiers: ["ONE", "TWO", "SOME", "ALL", "MUCH", "LITTLE"],
  evaluators: ["GOOD", "BAD"],
  descriptors: ["BIG", "SMALL"],
  mental: ["THINK", "KNOW", "WANT", "DONT_WANT", "FEEL", "SEE", "HEAR"],
  speech: ["SAY", "WORDS", "TRUE"],
  actions: ["DO", "HAPPEN", "MOVE"],
  existence: ["BE_SOMEWHERE", "THERE_IS", "BE_SOMEONE"],
  life: ["LIVE", "DIE"],
  time: ["WHEN", "NOW", "BEFORE", "AFTER", "A_LONG_TIME", "A_SHORT_TIME", "FOR_SOME_TIME", "MOMENT"],
  space: ["WHERE", "HERE", "ABOVE", "BELOW", "FAR", "NEAR", "SIDE", "INSIDE", "TOUCH"],
  logic: ["NOT", "MAYBE", "CAN", "BECAUSE", "IF"],
  intensifier: ["VERY", "MORE"],
  similarity: ["LIKE"],
};

export const ALL_PRIMES: ReadonlySet<string> = new Set(Object.values(PRIMES).flat());

/** Deterministic keyword → prime map. Conservative: a hit means the idea plausibly
 *  RELATES to the prime, nothing stronger. */
const KEYWORDS: Record<string, string> = {
  // mental predicates
  know: "KNOW", knew: "KNOW", remember: "KNOW", recall: "KNOW", forget: "KNOW", learn: "KNOW", understand: "KNOW",
  think: "THINK", thought: "THINK", believe: "THINK", idea: "THINK", plan: "THINK", decide: "THINK", consider: "THINK",
  want: "WANT", need: "WANT", wish: "WANT", hope: "WANT", goal: "WANT", prefer: "WANT", intend: "WANT",
  feel: "FEEL", felt: "FEEL", love: "FEEL", hate: "FEEL", afraid: "FEEL", happy: "FEEL", sad: "FEEL", angry: "FEEL",
  worry: "FEEL", excited: "FEEL",
  see: "SEE", saw: "SEE", look: "SEE", watch: "SEE", show: "SEE",
  hear: "HEAR", heard: "HEAR", listen: "HEAR", sound: "HEAR",
  // speech
  say: "SAY", said: "SAY", tell: "SAY", told: "SAY", ask: "SAY", asked: "SAY", talk: "SAY", call: "SAY", name: "SAY",
  word: "WORDS", true: "TRUE", false: "TRUE", correct: "TRUE", wrong: "TRUE", fact: "TRUE",
  // actions & events
  do: "DO", did: "DO", make: "DO", made: "DO", build: "DO", built: "DO", create: "DO", work: "DO", run: "DO", use: "DO",
  happen: "HAPPEN", happened: "HAPPEN", occur: "HAPPEN", event: "HAPPEN", became: "HAPPEN",
  move: "MOVE", go: "MOVE", went: "MOVE", come: "MOVE", leave: "MOVE", arrive: "MOVE", travel: "MOVE", drive: "MOVE",
  // people & things
  i: "I", me: "I", my: "I", mine: "I", myself: "I",
  you: "YOU", your: "YOU",
  people: "PEOPLE", person: "SOMEONE", who: "SOMEONE", someone: "SOMEONE", somebody: "SOMEONE",
  wife: "SOMEONE", husband: "SOMEONE", friend: "SOMEONE", family: "PEOPLE", team: "PEOPLE", kid: "SOMEONE",
  child: "SOMEONE", children: "PEOPLE", son: "SOMEONE", daughter: "SOMEONE", mother: "SOMEONE", father: "SOMEONE",
  something: "SOMETHING", thing: "SOMETHING", what: "SOMETHING",
  body: "BODY", hand: "BODY", head: "BODY", health: "BODY",
  // evaluation & description
  good: "GOOD", great: "GOOD", best: "GOOD", right: "GOOD", better: "GOOD", favorite: "GOOD",
  bad: "BAD", worst: "BAD", worse: "BAD", problem: "BAD", broken: "BAD", fail: "BAD", bug: "BAD", error: "BAD",
  big: "BIG", large: "BIG", huge: "BIG",
  small: "SMALL", little: "LITTLE", tiny: "SMALL",
  // time
  when: "WHEN", time: "WHEN", date: "WHEN", year: "WHEN", month: "WHEN", week: "WHEN", day: "WHEN", schedule: "WHEN",
  now: "NOW", today: "NOW", currently: "NOW",
  before: "BEFORE", ago: "BEFORE", yesterday: "BEFORE", was: "BEFORE", were: "BEFORE", past: "BEFORE", history: "BEFORE",
  after: "AFTER", tomorrow: "AFTER", next: "AFTER", will: "AFTER", future: "AFTER", soon: "AFTER",
  always: "A_LONG_TIME", forever: "A_LONG_TIME", never: "NOT",
  // space
  where: "WHERE", place: "WHERE", location: "WHERE", address: "WHERE", city: "WHERE", home: "WHERE", house: "WHERE",
  here: "HERE", near: "NEAR", close: "NEAR", far: "FAR", inside: "INSIDE", in: "INSIDE",
  // existence & life
  is: "THERE_IS", are: "THERE_IS", exist: "THERE_IS", have: "THERE_IS", has: "THERE_IS", own: "THERE_IS",
  live: "LIVE", life: "LIVE", born: "LIVE",
  die: "DIE", died: "DIE", death: "DIE", dead: "DIE",
  // quantity
  one: "ONE", two: "TWO", some: "SOME", few: "SOME",
  all: "ALL", every: "ALL", everything: "ALL",
  many: "MUCH", much: "MUCH", most: "MUCH", lot: "MUCH", count: "MUCH", number: "MUCH",
  // logic & modality
  not: "NOT", no: "NOT", none: "NOT", without: "NOT",
  maybe: "MAYBE", might: "MAYBE", perhaps: "MAYBE", could: "MAYBE", possibly: "MAYBE",
  can: "CAN", able: "CAN", cannot: "NOT",
  because: "BECAUSE", why: "BECAUSE", reason: "BECAUSE", since: "BECAUSE", cause: "BECAUSE",
  if: "IF", unless: "IF", whether: "IF",
  very: "VERY", really: "VERY", extremely: "VERY",
  more: "MORE", less: "MORE",
  like: "LIKE", similar: "LIKE", same: "SAME", as: "LIKE",
  different: "OTHER", other: "OTHER", another: "OTHER",
  this: "THIS", kind: "KIND", type: "KIND", part: "PART",
};

const WORD_RE = /[a-z']+/g;

/** Exact match, then light suffix-stripping (wants→want, moved→move, thinking→think).
 *  Deterministic — no stemmer dependency. */
function lookup(word: string): string | null {
  const direct = KEYWORDS[word];
  if (direct !== undefined) return direct;
  for (const suffix of ["ing", "ed", "es", "s"]) {
    if (word.endsWith(suffix) && word.length - suffix.length >= 3) {
      const stem = word.slice(0, word.length - suffix.length);
      const p = KEYWORDS[stem] ?? KEYWORDS[stem + "e"];
      if (p !== undefined) return p;
    }
  }
  return null;
}

/** Deterministic decomposition: text → the primes it relates to. Order = first
 *  appearance in the text (stable, reproducible). Numbers relate to quantity. */
export function decompose(text: string): string[] {
  const seen = new Set<string>();
  const lower = text.toLowerCase();
  for (const m of lower.matchAll(WORD_RE)) {
    const p = lookup(m[0]);
    if (p !== null) seen.add(p);
  }
  if (/\d/.test(text) && !seen.has("MUCH")) seen.add("MUCH");
  return [...seen];
}

/** Keep only real primes (a model may only choose, never invent). */
export function validate(primes: string[]): string[] {
  const out = new Set<string>();
  for (const raw of primes) {
    const p = raw.trim().toUpperCase().replace(/ /g, "_").replace(/-/g, "_");
    if (ALL_PRIMES.has(p)) out.add(p);
  }
  return [...out];
}

/** Heuristics + optional local-model sharpening. `complete` is async (prompt) →
 *  string|null; null (model down) → heuristics stand. Union, model ordering first —
 *  heuristics are never dropped. Caller checkpoints the model call for replay. */
export async function decomposeSharp(
  text: string,
  complete: (prompt: string) => Promise<string | null>,
): Promise<string[]> {
  const base = decompose(text);
  const raw = await complete(
    "You map text to semantic primes. From this closed list only:\n" +
      [...ALL_PRIMES].sort().join(", ") +
      "\n\nText:\n" +
      text.slice(0, 1200) +
      "\n\nReply with ONLY a comma-separated list of the 3-8 most relevant primes from the list. No other words.",
  );
  if (!raw) return base;
  const sharp = validate(raw.replace(/\n/g, ",").split(","));
  if (sharp.length === 0) return base;
  return validate([...sharp, ...base]);
}
