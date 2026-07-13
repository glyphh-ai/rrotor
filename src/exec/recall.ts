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
export function assembleRecall(memory: MemoryPlugin, query: string, opts: RecallOptions = {}): RecalledContext {
  const entity = opts.entity ?? "user";
  const directives = memory
    .recall({ entity, role: "directive", session: opts.session, midWindow: opts.midWindow, spaceId: opts.spaceId })
    .map((f) => f.filler);
  const recalled = query.trim() ? memory.semanticRecall(query, opts.topK ?? 5, opts.threshold ?? 0.08) : [];
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
