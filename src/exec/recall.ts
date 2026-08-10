/**
 * The RECALL assembler — the model-independent bridge that turns stored memory
 * into a context block for the next prompt (SPEC §15.1 WRITE → RECALL → REASON).
 *
 * This is what makes memory **transportable across vendors**: it reads only the
 * stator (standing directives + recorded turns), never a model, so a directive
 * given at turn 1 with one provider is recalled and injected at turn 20 with any
 * other provider — or a local model. Two modes:
 *
 *  - **Standing directives** — always injected, not similarity-filtered. "Always
 *    do X" must resurface even when the current turn shares no words with it.
 *  - **Semantic recall** — the top-k turns most similar to the current query, by
 *    the deterministic embedding (§7.7).
 */

import type { MemoryPlugin, SemanticHit } from "../plugins/interfaces.js";
import type { Fact } from "./facts.js";
import { decompose } from "./glyph/primes.js";

export interface RecallOptions {
  /** The directive-owning entity (the user/session). Default `user`. */
  entity?: string;
  /** The session recall runs in — scopes short/mid-tier memory (docs/memory.md). */
  session?: string;
  /** Mid-tier is visible within this many sessions. Default 5. */
  midWindow?: number;
  /** Semantic-recall breadth. Default 5. */
  topK?: number;
  /** Minimum cosine to include a semantic hit. Default 0.08. */
  threshold?: number;
  /** HDC space, when the rotor binds one. */
  spaceId?: string;
}

export interface RecalledContext {
  /** Always-injected standing instructions (mode 1). */
  directives: string[];
  /** Similarity hits for the current query (mode 2). */
  recalled: SemanticHit[];
}

/** Assemble the recall context for `query` from memory. Pure over the stator.
 *  Directives are tier-scoped to the session (docs/memory.md): `long` always,
 *  `mid` within the session window. */
export async function assembleRecall(memory: MemoryPlugin, query: string, opts: RecallOptions = {}): Promise<RecalledContext> {
  const entity = opts.entity ?? "user";
  const directives = (
    await memory.recall({ entity, role: "directive", session: opts.session, midWindow: opts.midWindow, spaceId: opts.spaceId })
  ).map((f) => f.filler);
  const recalled = query.trim() ? await memory.semanticRecall(query, opts.topK ?? 5, opts.threshold ?? 0.08) : [];
  return { directives, recalled };
}

/** Render the recall context as the text block prepended to the next prompt —
 *  provider-neutral (plain text any model consumes). */
export function recallBlock(ctx: RecalledContext): string {
  const parts: string[] = [];
  if (ctx.directives.length > 0) {
    parts.push("Standing instructions (always follow):\n" + ctx.directives.map((d) => `- ${d}`).join("\n"));
  }
  if (ctx.recalled.length > 0) {
    parts.push("Relevant earlier context:\n" + ctx.recalled.map((h) => `- ${h.text}`).join("\n"));
  }
  return parts.join("\n\n");
}

/** The FULL recall context for a turn: directives + semantic hits + the entity's
 *  fact node, rendered as one prompt block. The fact node reaches the model BY
 *  RIGHT (a name/degree/preference must not depend on cosine luck).
 *
 *  Two properties the raw fact table doesn't give you:
 *   - **Relevance-first, not recency-truncated.** The old block kept the last N
 *     facts by write order, so a fact stated early in a long history fell off the
 *     end. Here facts are ranked by overlap with the query, so the fact the turn
 *     is *about* is surfaced regardless of when it was stated. HDC already encodes
 *     a role's fillers no matter how many times they were stated; recall should
 *     not throw that away with a tail slice.
 *   - **Latest temporal value front and center.** Ties (and the residual after
 *     relevance) are ordered most-recent-first, so the current value of a role
 *     (e.g. an updated deadline/decision) leads.
 */
export interface FullRecall extends RecalledContext {
  facts: Fact[];
  block: string;
}

const _tok = (s: string): Set<string> =>
  new Set(s.toLowerCase().replace(/[^a-z0-9]+/g, " ").split(" ").filter((w) => w.length > 2));

function _rel(q: Set<string>, text: string): number {
  const t = _tok(text);
  let n = 0;
  for (const w of q) if (t.has(w)) n++;
  return n;
}

function _cos(a: number[], b: number[]): number {
  let d = 0, na = 0, nb = 0;
  for (let i = 0; i < a.length; i++) {
    d += a[i]! * b[i]!;
    na += a[i]! * a[i]!;
    nb += b[i]! * b[i]!;
  }
  return na && nb ? d / (Math.sqrt(na) * Math.sqrt(nb)) : 0;
}

export async function recallContext(
  memory: MemoryPlugin,
  query: string,
  opts: RecallOptions & { factCap?: number } = {},
): Promise<FullRecall> {
  const entity = opts.entity ?? "user";
  const ctx = await assembleRecall(memory, query, opts);
  const facts = await memory.recall({
    entity,
    ...(opts.spaceId ? { spaceId: opts.spaceId } : {}),
    ...(opts.session ? { session: opts.session } : {}),
    ...(opts.midWindow !== undefined ? { midWindow: opts.midWindow } : {}),
  });
  const cap = opts.factCap ?? 60;
  // Directives are their own section. Rank the rest in the SAME embedding space as
  // the turns — schema-on-write + embeddings compose: a fact is surfaced because it
  // MEANS the query, not because it shares a word. Falls back to lexical overlap if
  // the embedder is unavailable. Ties → most-recent-first (later index = newer).
  const nonDir = facts.map((f, i) => ({ f, i })).filter((x) => x.f.role !== "directive" && x.f.filler);
  // Prime lane: decompose the query to its NSM primes; a fact that shares primes
  // with the query is *about* the same thing (the fact tree), independent of
  // lexical or embedding overlap. Boost by the fraction of query primes the fact
  // covers. RROTOR_PRIME_WEIGHT tunes its pull relative to the neural cosine.
  const qPrimes = new Set(decompose(query));
  const primeWeight = Number(process.env.RROTOR_PRIME_WEIGHT) || 0.15;
  const primeBoost = (f: Fact): number => {
    if (qPrimes.size === 0) return 0;
    const fp = decompose(`${f.role} ${f.filler}`);
    let ov = 0;
    for (const p of fp) if (qPrimes.has(p)) ov++;
    return primeWeight * (ov / qPrimes.size);
  };
  let ordered = nonDir;
  if (nonDir.length > 0 && query.trim()) {
    try {
      const [qv, ...fvs] = await memory.embedBatch([query, ...nonDir.map((x) => `${x.f.role}: ${x.f.filler}`)]);
      ordered = nonDir
        .map((x, j) => ({ ...x, s: _cos(qv!, fvs[j]!) + primeBoost(x.f) }))
        .sort((a, b) => b.s - a.s || b.i - a.i);
    } catch {
      const q = _tok(query);
      ordered = [...nonDir].sort(
        (a, b) => _rel(q, `${b.f.role} ${b.f.filler}`) - _rel(q, `${a.f.role} ${a.f.filler}`) || b.i - a.i,
      );
    }
  } else {
    ordered = [...nonDir].sort((a, b) => b.i - a.i);
  }
  const factLines = ordered.slice(0, cap).map(({ f }) => `- ${f.entity} ${f.role}: ${f.filler}`);
  const block = [
    recallBlock(ctx),
    factLines.length ? `Known facts (most relevant / most recent first):\n${factLines.join("\n")}` : "",
  ]
    .filter(Boolean)
    .join("\n\n");
  return { directives: ctx.directives, recalled: ctx.recalled, facts, block };
}
