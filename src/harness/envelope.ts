/**
 * envelope.ts — PROGRAM-PER-TURN, the envelope half (P1).
 *
 * The session's LOOP is a PROGRAM, and from here it fires on every interactive
 * turn (server/docs/program-per-turn.md): the loop's `pre(ctx)` hook runs
 * deterministically BEFORE the SDK inner loop and shapes the turn — steer,
 * context budget, tool subset, or an outright refusal. The SDK run is the
 * stochastic step between deterministic phases; `post` and the turn tape are
 * P2.
 *
 * LAWS:
 *  • NARROW-ONLY — a plan can reduce what a turn may do, never widen it. The
 *    parser clamps every field; tools INTERSECT the mode's pack downstream.
 *  • FAIL-OPEN, LOUDLY — a missing/broken/slow hook skips (the turn runs on
 *    defaults) and a `setup{phase:"loop-pre"}` frame says so. A program bug
 *    must not brick every conversation in the org.
 *  • BOUNDED — module eval and the hook each get a small wall-clock budget.
 *    vm timeouts bound SYNC work; the async continuation is raced. (A hostile
 *    busy-loop after an await can still burn its pod's CPU slice — real
 *    isolation is a worker_threads follow-up, noted in the plan.)
 *
 * Hook contract (either style compiles):
 *   export async function pre(ctx) { return { steer: "…" }; }
 *   exports.pre = async (ctx) => ({ contextBudget: 30000 });
 */

import { runInNewContext } from "node:vm";

/** What `pre` may decide — every field optional, every field clamped. */
export interface TurnPlan {
  /** Appended to the turn's injection block (rides the fact-block lane). */
  steer?: string;
  /** Max transcript tokens to carry — can only LOWER the configured budget. */
  contextBudget?: number;
  /** Tool ALLOWLIST — intersected with the mode's pack downstream. */
  tools?: string[];
  /** Deterministic refusal: the turn never reaches the model. */
  refuse?: string;
}

/** What the hook sees. Deliberately small in P1: metadata, not transcripts. */
export interface PreCtx {
  prompt: string;
  mode: string;
  historyTurns: number;
  workdir?: string;
  loop: string;
}

/** What `post` sees (P2): the turn's outcome. `finalText` is capped — post is
 *  decision code over the RESULT, not a second transcript. */
export interface PostCtx extends PreCtx {
  finalText: string;
  error?: string;
  aborted: boolean;
  usage: { inTokens: number; outTokens: number };
  /** The plan `pre` returned this turn (null when none) — so post can check
   *  its own program's intent against the outcome. */
  plan: TurnPlan | null;
}

/** What `post` may DO — declarative effects, applied by the engine under the
 *  principal's authority. Each is a capability, not ambient power. */
export interface PostEffects {
  /** Facts to record on the org ledger (capped; the engine writes them). */
  facts?: Array<{ name: string; facts: unknown }>;
  /** A short annotation framed into the transcript (setup loop-post frame). */
  note?: string;
}

export interface LoopHooks {
  pre?: (ctx: PreCtx) => unknown;
  post?: (ctx: PostCtx) => unknown;
}

const NOTE_MAX = 1_000;
const FACTS_MAX = 5;
const FINAL_TEXT_CAP = 8_000;

/** Clamp an untrusted post return into effects. Unknown fields drop. */
export function parsePostEffects(v: unknown): PostEffects | null {
  if (!v || typeof v !== "object") return null;
  const o = v as Record<string, unknown>;
  const out: PostEffects = {};
  if (typeof o.note === "string" && o.note.trim()) out.note = o.note.trim().slice(0, NOTE_MAX);
  if (Array.isArray(o.facts)) {
    const facts = o.facts
      .filter((f): f is Record<string, unknown> => !!f && typeof f === "object")
      .filter((f) => typeof f.name === "string" && !!(f.name as string).trim())
      .slice(0, FACTS_MAX)
      .map((f) => ({ name: String(f.name).trim().slice(0, 120), facts: f.facts }));
    if (facts.length) out.facts = facts;
  }
  return Object.keys(out).length ? out : null;
}

/** Cap what post reads of the reply. */
export function capFinalText(text: string): string {
  return text.length > FINAL_TEXT_CAP ? `${text.slice(0, FINAL_TEXT_CAP)}\n[⋯ capped]` : text;
}

/** Run `post` under the same budget law as pre. Never throws. */
export async function runPost(
  hooks: LoopHooks,
  ctx: PostCtx,
  budgetMs: number,
): Promise<{ effects: PostEffects | null; ms: number; error?: string }> {
  const t0 = Date.now();
  if (!hooks.post) return { effects: null, ms: 0 };
  try {
    const raced = await Promise.race([
      Promise.resolve(hooks.post(ctx)),
      new Promise<never>((_, rej) => setTimeout(() => rej(new Error(`post exceeded ${budgetMs}ms`)), budgetMs)),
    ]);
    return { effects: parsePostEffects(raced), ms: Date.now() - t0 };
  } catch (err) {
    return { effects: null, ms: Date.now() - t0, error: (err as Error).message };
  }
}

const STEER_MAX = 2_000;
const REFUSE_MAX = 500;
const TOOLS_MAX = 64;
/** Module evaluation budget (sync). */
const COMPILE_MS = 25;

/** Clamp an untrusted hook return into a TurnPlan. Unknown fields drop. */
export function parseTurnPlan(v: unknown): TurnPlan | null {
  if (!v || typeof v !== "object") return null;
  const o = v as Record<string, unknown>;
  const plan: TurnPlan = {};
  if (typeof o.steer === "string" && o.steer.trim()) plan.steer = o.steer.trim().slice(0, STEER_MAX);
  if (typeof o.contextBudget === "number" && Number.isFinite(o.contextBudget) && o.contextBudget > 0) {
    plan.contextBudget = Math.floor(o.contextBudget);
  }
  if (Array.isArray(o.tools)) {
    const tools = o.tools.filter((t): t is string => typeof t === "string" && !!t.trim()).map((t) => t.trim()).slice(0, TOOLS_MAX);
    if (tools.length) plan.tools = tools;
  }
  if (typeof o.refuse === "string" && o.refuse.trim()) plan.refuse = o.refuse.trim().slice(0, REFUSE_MAX);
  return Object.keys(plan).length ? plan : null;
}

/** The documented contract is ESM-flavored (`export async function pre`); vm
 *  speaks script, not module. A tiny mechanical transform maps the exports the
 *  envelope knows about onto `exports.*` — everything else in the file runs
 *  as-is. CJS-style programs pass through untouched. */
export function toScript(code: string): string {
  return code
    .replace(/export\s+(async\s+)?function\s+(pre|post)\b/g, "exports.$2 = $1function $2")
    .replace(/export\s+const\s+(mode)\s*=/g, "exports.$1 =")
    .replace(/export\s+default\s+/g, "exports.default = ");
}

/**
 * Evaluate a loop program and pull its hooks. Sandboxed: no require, no
 * process, no timers — a hook is DECISION code. Returns null when the program
 * doesn't parse or exports no hooks (a bindings-only loop — the common case).
 */
export function compileHooks(code: string): LoopHooks | null {
  const exportsObj: Record<string, unknown> = {};
  const sandbox = {
    exports: exportsObj,
    module: { exports: exportsObj },
    console: { log: () => {}, warn: () => {}, error: () => {} },
  };
  try {
    // NO microtaskMode: an async hook is CALLED after evaluation, and
    // "afterEvaluate" would strand its continuation in a context queue that
    // never drains again — the promise would simply never settle. The hooks
    // share the caller's microtask queue; runPre's race bounds them.
    runInNewContext(`"use strict";\n${toScript(code)}`, sandbox, { timeout: COMPILE_MS });
  } catch {
    return null; // a program that doesn't evaluate has no hooks — fail-open
  }
  const mod = (sandbox.module.exports ?? exportsObj) as Record<string, unknown>;
  const pre = typeof mod.pre === "function" ? (mod.pre as LoopHooks["pre"]) : undefined;
  const post = typeof mod.post === "function" ? (mod.post as LoopHooks["post"]) : undefined;
  return pre || post ? { ...(pre ? { pre } : {}), ...(post ? { post } : {}) } : null;
}

/** Run `pre` under a wall-clock budget. Never throws: an error or timeout is
 *  reported so the caller can frame it — and the turn proceeds on defaults. */
export async function runPre(
  hooks: LoopHooks,
  ctx: PreCtx,
  budgetMs: number,
): Promise<{ plan: TurnPlan | null; ms: number; error?: string }> {
  const t0 = Date.now();
  if (!hooks.pre) return { plan: null, ms: 0 };
  try {
    const raced = await Promise.race([
      Promise.resolve(hooks.pre(ctx)),
      new Promise<never>((_, rej) => setTimeout(() => rej(new Error(`pre exceeded ${budgetMs}ms`)), budgetMs)),
    ]);
    return { plan: parseTurnPlan(raced), ms: Date.now() - t0 };
  } catch (err) {
    return { plan: null, ms: Date.now() - t0, error: (err as Error).message };
  }
}

// ── Program cache — the turn path never waits on a cold read twice ───────────
// TTL like the desktop's loop-selections cache; last-good is BOUNDED (the
// unbounded version served a stale allow-list for days — the 402 of 2026-09-04).
const TTL_MS = 60_000;
const LAST_GOOD_TTL_MS = 10 * 60_000;
interface CacheEntry { hooks: LoopHooks | null; at: number }
const cache = new Map<string, CacheEntry>();

/**
 * Resolve a loop's hooks through `load` (the store read), cached per org+ref.
 * A failed refresh degrades to bounded-stale; past that, null (no hooks).
 */
export async function cachedHooks(
  key: string,
  load: () => Promise<string | null>,
): Promise<LoopHooks | null> {
  const hit = cache.get(key);
  const now = Date.now();
  if (hit && now - hit.at < TTL_MS) return hit.hooks;
  try {
    const code = await load();
    const hooks = code ? compileHooks(code) : null;
    cache.set(key, { hooks, at: now });
    return hooks;
  } catch {
    if (hit && now - hit.at < LAST_GOOD_TTL_MS) return hit.hooks;
    return null;
  }
}

/** Test seam. */
export function clearEnvelopeCache(): void { cache.clear(); }
