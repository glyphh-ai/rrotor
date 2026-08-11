/**
 * harness/transcript.ts — the FULL-CONTEXT FALLBACK: per-session transcript
 * retention + first-class compaction for runs where the memory rotor is NOT
 * active.
 *
 * THE LAW (docs/memory.md is the premium lane; this is the floor): when memory
 * is not active — no principal, no stator, rotor off — the worker must still
 * carry the COMPLETE conversation, every turn verbatim, compacted (never
 * silently truncated) when it outgrows the window. Memory-off must NEVER mean
 * thread-loss. The real incident this closes: a cloud session built + published
 * an app, then next turn claimed "my workspace is empty" because the rotor was
 * opt-in-off and the per-run prompt carried only what the client happened to
 * resend.
 *
 * Mechanism, mirroring the docs' determinism rules:
 *
 *   RETENTION — the pod keeps ONE transcript per thread key (threadId ??
 *   sessionId): each completed run appends its user prompt + assistant answer.
 *   Appends are RECORD-ONCE per runId, so a retried/replayed run never
 *   double-appends (the harness analogue of the StepRecord idempotency key).
 *   In-memory v1, like the FrameRing — a pod restart is reconciled by
 *   `adopt()`: the client's resent history reseeds the store when it carries
 *   MORE turns than the pod retains, and a SHORTER client history never
 *   shrinks the pod's fuller record (partial client history WAS the incident).
 *
 *   COMPACTION — when the rendered transcript would exceed the token budget,
 *   the OLDEST turns fold into one summary block via a cheap back-path LLM
 *   call (the enricher pattern: stochastic data, checkpointed at the boundary
 *   — the summary is STORED in the transcript state, so replay/repeat reads
 *   the record and never re-invokes the model). The newest `keepTurns` turns
 *   stay VERBATIM. A failed/absent summarizer degrades to a deterministic
 *   gist fold — degraded fidelity, never a lost thread.
 *
 *   ANCHORS — durable facts a compaction must never drop are harvested
 *   DETERMINISTICALLY (regex, not model judgment) from every turn at append
 *   time: URLs, published-app hosts/slugs, absolute file paths — plus the
 *   session goal (the first user turn). They are rendered verbatim alongside
 *   the summary, so the facts needed to continue the work survive by
 *   construction even if the summarizer paraphrases them away.
 *
 * The engine (engine.ts) engages this ONLY when the rotor is not active; a
 * rotor-on turn keeps current behavior (the rotor owns the system prompt) and
 * this store just retains turns so a later rotor-off turn still has the thread.
 */

import type { ChatTurn } from "./config.js";
import { log } from "../obs/logger.js";

/** Sizing defaults — overridable per run (body `context`) or env
 *  (HARNESS_CONTEXT_BUDGET_TOKENS / HARNESS_CONTEXT_KEEP_TURNS). */
export const CONTEXT_DEFAULTS = {
  /** Transcript token budget before compaction folds the oldest turns. */
  budgetTokens: 32_000,
  /** Newest turns ALWAYS kept verbatim through a compaction. */
  keepTurns: 8,
};

/** Cheap deterministic token estimate (chars/4) — sizing, not billing. */
export function estimateTokens(text: string): number {
  return Math.ceil(text.length / 4);
}

/** One thread's retained state. `summary`+`folded` are the compaction record
 *  (record-once); `turns` is the verbatim tail; `anchors`/`goal` are the
 *  deterministic never-drop set. */
interface TranscriptState {
  turns: ChatTurn[];
  summary: string;
  folded: number;
  anchors: string[];
  goal: string;
  lastRunId?: string;
}

/** What a compaction did — the engine emits it as a frame + log line. */
export interface CompactionEvent {
  /** Turns folded into the summary by THIS compaction. */
  folded: number;
  /** Verbatim turns kept. */
  kept: number;
  /** Rendered summary size. */
  summaryChars: number;
  /** `model` = the summarizer LLM produced it; `deterministic` = the gist fallback. */
  via: "model" | "deterministic";
}

/** The compaction summarizer's system prompt (the back-path LLM call). */
export const COMPACTION_SYSTEM = [
  "You compact the OLDEST turns of a conversation into a dense continuation briefing",
  "for an agent that must keep working on the same task. Output ONLY the briefing.",
  "PRESERVE, verbatim, never paraphrased away: the session's goal; every artifact",
  "produced (app names, slugs, URLs, file paths); every decision made and constraint",
  "given; current state of the work. Fold pleasantries and dead ends to nothing.",
  "If a PRIOR SUMMARY is given, carry its facts forward — it is earlier history.",
].join("\n");

const MAX_ANCHORS = 100;
const GOAL_CHARS = 300;
const GIST_CHARS = 160;

const URL_RE = /https?:\/\/[^\s"')\]>,;]+/g;
const APP_HOST_RE = /\b[a-z0-9][a-z0-9-]*\.glyphh\.app\b/g;
const ABS_PATH_RE = /(?:^|[\s"'`(=])((?:\/[A-Za-z0-9._-]+){2,})/g;
const SLUG_RE = /\bslugs?\s*[:=]?\s*["'`]?([a-z0-9][a-z0-9-]{2,})/gi;

/** Deterministically harvest durable anchors from one turn's text. */
export function harvestAnchors(text: string): string[] {
  const found: string[] = [];
  for (const m of text.match(URL_RE) ?? []) found.push(m.replace(/[.,]+$/, ""));
  for (const m of text.match(APP_HOST_RE) ?? []) found.push(m);
  for (const m of text.matchAll(ABS_PATH_RE)) found.push(m[1].replace(/[.,;:]+$/, ""));
  for (const m of text.matchAll(SLUG_RE)) found.push(`slug ${m[1]}`);
  return found;
}

function turnLine(t: ChatTurn): string {
  return `${t.role === "user" ? "User" : "Glyphh"}: ${t.content}`;
}

/** Render one thread's context block (the `<conversation_so_far>` body).
 *  Anchors ride only once something has been folded — while every turn is
 *  verbatim they are already present in the text. */
export function renderContext(state: { summary: string; folded: number; turns: ChatTurn[]; anchors: string[]; goal: string }): string {
  const parts: string[] = [];
  if (state.folded > 0) {
    parts.push(`[Compacted summary of the ${state.folded} earliest turns]\n${state.summary}`);
    const anchorLines = [
      ...(state.goal ? [`Session goal: ${state.goal}`] : []),
      ...state.anchors,
    ];
    if (anchorLines.length) {
      parts.push(`[Durable anchors — verbatim, these facts survive compaction]\n${anchorLines.map((a) => `- ${a}`).join("\n")}`);
    }
    if (state.turns.length) parts.push(`[Most recent turns, verbatim]\n${state.turns.map(turnLine).join("\n")}`);
  } else if (state.turns.length) {
    parts.push(state.turns.map(turnLine).join("\n"));
  }
  return parts.join("\n\n");
}

/** Options for {@link TranscriptStore.compact}. `summarize` is the back-path
 *  LLM seam; absent or failing, the deterministic gist fold runs instead. */
export interface CompactOptions {
  budgetTokens: number;
  keepTurns: number;
  summarize?: (input: string) => Promise<string>;
}

/**
 * The pod's per-thread transcript retention + compaction store. In-memory v1
 * (the FrameRing pattern); `adopt()` reconciles a pod restart from the
 * client's resent history.
 */
export class TranscriptStore {
  private readonly sessions = new Map<string, TranscriptState>();

  private state(key: string): TranscriptState {
    let s = this.sessions.get(key);
    if (!s) {
      s = { turns: [], summary: "", folded: 0, anchors: [], goal: "" };
      this.sessions.set(key, s);
    }
    return s;
  }

  private noteAnchors(s: TranscriptState, text: string): void {
    for (const a of harvestAnchors(text)) {
      if (!s.anchors.includes(a) && s.anchors.length < MAX_ANCHORS) s.anchors.push(a);
    }
  }

  private noteGoal(s: TranscriptState, turn: ChatTurn): void {
    if (!s.goal && turn.role === "user" && turn.content.trim()) {
      s.goal = turn.content.replace(/\s+/g, " ").trim().slice(0, GOAL_CHARS);
    }
  }

  /**
   * Reconcile the client's resent history with the pod's retained transcript.
   * The FULLER record wins: a client carrying MORE turns than the pod retains
   * (pod restart — the in-memory store died) reseeds the state; a client
   * carrying fewer/none (the incident: partial client history) changes
   * nothing — the pod's record stands.
   */
  adopt(key: string, clientHistory: ChatTurn[]): void {
    const s = this.state(key);
    const retained = s.folded + s.turns.length;
    if (clientHistory.length <= retained) return;
    // Reseed. The folded summary (if any) predates what the client carries
    // only when the pod outlived it — here the pod lost state, so the client
    // IS the thread. Harvest anchors/goal from every adopted turn.
    s.turns = clientHistory.map((t) => ({ role: t.role, content: t.content }));
    s.summary = "";
    s.folded = 0;
    s.anchors = [];
    s.goal = "";
    for (const t of s.turns) {
      this.noteGoal(s, t);
      this.noteAnchors(s, t.content);
    }
    log.info("transcript adopted from client history", { thread: key, turns: s.turns.length });
  }

  /** Append one completed exchange. RECORD-ONCE per runId — a replayed/retried
   *  run appends nothing the second time (idempotent, replay-safe). */
  append(key: string, runId: string, userPrompt: string, assistantText: string): void {
    const s = this.state(key);
    if (s.lastRunId === runId) return;
    s.lastRunId = runId;
    const user: ChatTurn = { role: "user", content: userPrompt };
    this.noteGoal(s, user);
    this.noteAnchors(s, userPrompt);
    s.turns.push(user);
    if (assistantText.trim()) {
      this.noteAnchors(s, assistantText);
      s.turns.push({ role: "assistant", content: assistantText });
    }
  }

  /** The rendered context block for a thread ("" when nothing is retained). */
  render(key: string): string {
    return renderContext(this.state(key));
  }

  /** Turns retained verbatim (tests + diagnostics). */
  turnCount(key: string): number {
    return this.state(key).turns.length;
  }

  /**
   * Compact when the rendered transcript exceeds the budget: fold the oldest
   * turns (all but the newest `keepTurns`) into the summary. The summarizer's
   * output is STORED (record-once, the enricher pattern) — a repeat call sees
   * the folded state and does not re-invoke the model. Returns what happened,
   * or null when no compaction was needed/possible. Never throws.
   */
  async compact(key: string, opts: CompactOptions): Promise<CompactionEvent | null> {
    const s = this.state(key);
    const budget = Math.max(1, opts.budgetTokens);
    const keep = Math.max(1, opts.keepTurns);
    if (estimateTokens(renderContext(s)) <= budget) return null;
    const foldCount = s.turns.length - keep;
    if (foldCount <= 0) {
      // The verbatim floor: the recent turns alone exceed the budget. They are
      // NEVER dropped — the thread survives over-budget rather than lossy.
      log.warn("transcript over budget but under keepTurns — kept verbatim", { thread: key, turns: s.turns.length });
      return null;
    }
    const folded = s.turns.slice(0, foldCount);
    const foldedText = folded.map(turnLine).join("\n");
    const input = [
      s.summary ? `### PRIOR SUMMARY (earlier history, carry its facts forward)\n${s.summary}` : "",
      `### TURNS TO COMPACT (oldest first)\n${foldedText}`,
      s.anchors.length || s.goal
        ? `### MUST SURVIVE VERBATIM\n${[...(s.goal ? [`goal: ${s.goal}`] : []), ...s.anchors].join("\n")}`
        : "",
    ].filter(Boolean).join("\n\n");

    let summary = "";
    let via: CompactionEvent["via"] = "deterministic";
    if (opts.summarize) {
      try {
        summary = (await opts.summarize(input)).trim();
        if (summary) via = "model";
      } catch (err) {
        log.warn("compaction summarizer failed; deterministic gist fold", { thread: key, detail: (err as Error).message });
      }
    }
    if (!summary) {
      // Deterministic fallback: one gist line per folded turn (the memory
      // rotor's fidelity-ladder rung 1). Anchors ride separately, verbatim.
      const gists = folded.map((t) => {
        const flat = t.content.replace(/\s+/g, " ").trim();
        return `(${t.role}, earlier) ${flat.slice(0, GIST_CHARS)}${flat.length > GIST_CHARS ? "…" : ""}`;
      });
      summary = [s.summary, ...gists].filter(Boolean).join("\n");
    }

    s.summary = summary;
    s.folded += foldCount;
    s.turns = s.turns.slice(foldCount);
    return { folded: foldCount, kept: s.turns.length, summaryChars: summary.length, via };
  }

  /** Drop everything (tests). */
  reset(): void {
    this.sessions.clear();
  }
}

/** The pod-wide store instance (one per process, keyed by thread). */
export const sessionTranscripts = new TranscriptStore();

/**
 * Build the back-path summarizer against the metered gateway (Anthropic
 * Messages; x-api-key = the run's token; x-glyphh-run correlates the spend —
 * the callGatewayAssembler pattern). Model from HARNESS_COMPACTION_MODEL
 * (default claude-haiku-4-5). `fetchFn` is the injectable test seam.
 */
export function gatewaySummarizer(opts: {
  gatewayUrl: string;
  token: string;
  runId?: string;
  fetchFn?: typeof fetch;
  env?: NodeJS.ProcessEnv;
}): (input: string) => Promise<string> {
  const env = opts.env ?? process.env;
  const model = env.HARNESS_COMPACTION_MODEL ?? "claude-haiku-4-5";
  const maxTokens = Number(env.HARNESS_COMPACTION_MAX_TOKENS) || 1500;
  const timeoutMs = Number(env.HARNESS_COMPACTION_TIMEOUT) || 30_000;
  const fetchFn = opts.fetchFn ?? fetch;
  const base = opts.gatewayUrl.replace(/\/+$/, "");
  const url = base.endsWith("/v1") ? `${base}/messages` : `${base}/v1/messages`;
  return async (input: string): Promise<string> => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    (timer as { unref?: () => void }).unref?.();
    try {
      const res = await fetchFn(url, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-api-key": opts.token,
          "anthropic-version": "2023-06-01",
          ...(opts.runId ? { "x-glyphh-run": opts.runId } : {}),
        },
        body: JSON.stringify({ model, max_tokens: maxTokens, system: COMPACTION_SYSTEM, messages: [{ role: "user", content: input }] }),
        signal: ctrl.signal,
      });
      if (!res.ok) throw new Error(`compaction: gateway ${res.status}`);
      const json = (await res.json()) as { content?: Array<{ type: string; text?: string }> };
      return (json.content ?? []).filter((b) => b.type === "text" && b.text).map((b) => b.text as string).join("");
    } finally {
      clearTimeout(timer);
    }
  };
}
