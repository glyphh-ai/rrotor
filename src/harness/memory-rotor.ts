/**
 * harness/memory-rotor.ts — the memory rotor (v0).
 *
 * One job: on every turn, produce the WORKER's system prompt — a dynamic,
 * per-turn briefing, sized to the window and saturated with short/mid/long-term
 * memory. The worker LLM sees only this system prompt + the user's UNTOUCHED
 * prompt. Everything rrotor knows about the session is compressed into this one
 * artifact ("Look here / Do this / Don't do that"). See docs/recursive-memory.md.
 *
 * This is a STOCHASTIC-DATA component (like the enricher): a small cheap LLM
 * assembles the prompt. It is opt-in and sits BESIDE the deterministic core loop,
 * never inside it; its output is checkpointable at the boundary (the return value)
 * so replay stays stable.
 *
 * v0 is deliberately dumb — so we can measure before we optimise:
 *   - short-term: a sliding turn buffer with a fidelity ladder (raw recent → gist).
 *   - mid-term:   cached standards text, passed in (a shipped baseline / rules).
 *   - long-term:  recallContext over the stator (schema-on-write facts + neural +
 *                 HDC prime overlap). No forest tree YET — recall stands in for it.
 * The assembler is a small model (default claude-haiku-4-5).
 *
 * Config:
 *   MEMORY_ROTOR_MODEL_URL — base (default Anthropic). Anthropic (…anthropic.com
 *                            or a claude-* id) → Messages API; else OpenAI-compat.
 *   MEMORY_ROTOR_MODEL_ID  — model id (default `claude-haiku-4-5`).
 *   MEMORY_ROTOR_API_KEY   — bearer (Anthropic: falls back to ANTHROPIC_API_KEY).
 *   MEMORY_ROTOR_MAX_TOKENS — worker-prompt token cap (default 1200).
 *   MEMORY_ROTOR_TIMEOUT   — ms budget for the construction call (default 30000).
 */

import { recallContext } from "../exec/recall.js";
import { enrichFacts } from "./enricher.js";
import { absorbText } from "../handlers/memory.js";
import type { MemoryPlugin } from "../plugins/interfaces.js";
import { log } from "../obs/logger.js";

/** The memory rotor's OWN system prompt — instructs the cheap LLM how to build
 *  the worker's system prompt. Directive, minimal-sufficient, prompt-untouched. */
export const MEMORY_ROTOR_SYSTEM = [
  "You are the MEMORY ROTOR, and your ONE job is ATTENTION: keep the worker LLM",
  "anchored to the session's overall goal so it does not drift into the weeds. Your",
  "only output is the SYSTEM PROMPT for a separate worker that answers the user THIS",
  "turn. You do not answer the user.",
  "",
  "You are given:",
  "  1. PRIMARY FOCUS — the session's overall goal (the north star). May be empty; if",
  "     so, INFER it from RECENT CONTEXT + the USER PROMPT and state it plainly.",
  "  2. STANDARDS — cached rules/best-practices that always apply.",
  "  3. LONG-TERM MEMORY — durable facts recalled from earlier (may be empty).",
  "  4. RECENT CONTEXT — the last few turns, BOTH the user's prompts AND the worker's",
  "     responses, most-recent last. This is the live thread; carry it.",
  "  5. USER PROMPT — the user's exact request for this turn.",
  "",
  "Write the worker's system prompt as a TIGHT briefing, ATTENTION FIRST:",
  "  ## Focus — the one goal this turn serves and where we are relative to it. Lead",
  "    with this; everything below serves it.",
  "  ## Look here — only the context/facts that serve the focus, and nothing else.",
  "  ## Do this  — how to approach THIS request toward the goal + the standards that apply.",
  "  ## Don't do that — guardrails, learned aversions, and anything that pulls off-goal.",
  "",
  "Hard rules:",
  "- The FOCUS is the point. Filter everything through it. If recalled memory does not",
  "  serve the goal, DROP it — breadth that dilutes focus is the failure mode.",
  "- MINIMAL-SUFFICIENT, not maximal. Only what grounds THIS turn toward the goal.",
  "- NEVER restate, rewrite, paraphrase, or answer the USER PROMPT. It reaches the",
  "  worker verbatim, separately. You build the scaffolding around it, not it.",
  "- Only cite memory/context you were given. Never invent facts, files, or history.",
  "- If a section has nothing worth saying, omit it. Prose, no preamble.",
  "Output ONLY the worker system prompt.",
].join("\n");

export interface Turn {
  role: string;
  text: string;
}

/**
 * A billable usage record emitted for EVERY memory-rotor model call. rrotor
 * reports tokens + the model id only — it never prices; the host applies the
 * governed rate card for `model` and attributes the credit burn to the org/user.
 * `service: "memory-rotor"` keeps this a distinct, reportable line from worker
 * turns. This is the traceability seam for metered memory.
 */
export interface MemoryUsage {
  service: "memory-rotor";
  /** Which phase burned the tokens (back-path warm vs foreground construct). */
  phase: "warm" | "construct";
  /** The governed model id whose rate card the host prices this at. */
  model: string;
  inputTokens: number;
  outputTokens: number;
  /** Attribution: the session this memory turn served (org/user resolved by host). */
  sessionId?: string;
}

export interface MemoryRotorOptions {
  /** The directive-owning entity (the user/session). Default `user`. */
  entity?: string;
  /** The assembler model id (a governed catalog model). Falls back to
   *  MEMORY_ROTOR_MODEL_ID, then `claude-haiku-4-5`. */
  model?: string;
  /** Session id stamped on every {@link MemoryUsage} record for attribution. */
  sessionId?: string;
  /** Metering sink — invoked once per model call with the billable record. The
   *  host wires this to its credit/metering pipeline (rrotor stays price-agnostic). */
  onUsage?: (u: MemoryUsage) => void;
  /** Turns kept fully VERBATIM at the tail (rung 0). Default 2. */
  rawTurns?: number;
  /** Turns before the raw window kept as a one-line GIST (rung 1). Default 4. */
  gistTurns?: number;
  /** Chars per gist line. Default 160. */
  gistChars?: number;
  /** Cached mid-term standards/rules text (the shipped baseline). */
  standards?: string;
  /** Semantic-recall breadth for long-term. Default 6. */
  topK?: number;
}

export interface ConstructedPrompt {
  /** The worker's system prompt — the single artifact the rotor produces. */
  systemPrompt: string;
  /** What went in, for checkpointing / auditing (provenance). */
  parts: { standards: string; longTerm: string; shortTerm: string; userPrompt: string };
  /** The billable record for this construction (also delivered via onUsage). */
  usage: MemoryUsage;
}

/**
 * The memory rotor: observe turns (schema-on-write + a sliding fidelity buffer),
 * then construct the worker's system prompt for a given user prompt.
 */
export class MemoryRotor {
  private readonly buffer: Turn[] = [];
  /** The cached brief from the last back-path warm() — the next turn's system
   *  prompt, minus the (untouched) user-prompt slot and its this-turn recall delta. */
  private warmBrief?: string;
  private readonly model: string;
  private readonly sessionId?: string;
  private readonly onUsage?: (u: MemoryUsage) => void;
  private readonly opts: {
    entity: string; rawTurns: number; gistTurns: number; gistChars: number; standards: string; topK: number;
  };

  constructor(
    private readonly memory: MemoryPlugin,
    opts: MemoryRotorOptions = {},
  ) {
    this.model = opts.model ?? process.env.MEMORY_ROTOR_MODEL_ID ?? "claude-haiku-4-5";
    this.sessionId = opts.sessionId;
    this.onUsage = opts.onUsage;
    this.opts = {
      entity: opts.entity ?? "user",
      rawTurns: opts.rawTurns ?? 2,
      gistTurns: opts.gistTurns ?? 4,
      gistChars: opts.gistChars ?? 160,
      standards: opts.standards ?? "",
      topK: opts.topK ?? 6,
    };
  }

  /** Preload prior context: push to the buffer and (for user turns) distil to
   *  facts. Use for bulk history load; the live loop uses next()/warm() instead. */
  async observe(role: string, text: string, session?: string): Promise<void> {
    this.buffer.push({ role, text });
    if (role === "user") await this.distill(text, session);
  }

  /** Schema-on-write one user turn → tiered facts in the stator. Back-path work
   *  (an LLM enricher call). Best-effort: a memory write never throws into a turn. */
  private async distill(text: string, session?: string): Promise<void> {
    if (!text.trim()) return;
    try {
      const facts = (await enrichFacts(text, this.opts.entity)) ?? absorbText(text, this.opts.entity);
      await this.memory.write(facts as Array<Record<string, unknown>>, {
        key: this.opts.entity,
        mode: "absorb",
        speaker: "user",
        session,
      });
    } catch (err) {
      log.warn("memory_rotor.distill.write_failed", { error: err instanceof Error ? err.message : String(err) });
    }
  }

  /** Render the short-term buffer as a fidelity ladder: the last `rawTurns` are
   *  verbatim (rung 0); the `gistTurns` before them are one-line gists (rung 1);
   *  anything older is left to long-term recall (it is already facts). */
  private renderShortTerm(): string {
    const { rawTurns, gistTurns, gistChars } = this.opts;
    const n = this.buffer.length;
    const rawStart = Math.max(0, n - rawTurns);
    const gistStart = Math.max(0, rawStart - gistTurns);
    const lines: string[] = [];
    for (let i = gistStart; i < rawStart; i++) {
      const t = this.buffer[i]!;
      const gist = t.text.replace(/\s+/g, " ").trim().slice(0, gistChars);
      lines.push(`(${t.role}, earlier) ${gist}${t.text.length > gistChars ? "…" : ""}`);
    }
    for (let i = rawStart; i < n; i++) {
      const t = this.buffer[i]!;
      lines.push(`${t.role}: ${t.text}`);
    }
    return lines.join("\n");
  }

  /** Assemble the brief via the assembler LLM. `userPrompt` is null in the
   *  back-path warm case (the next turn isn't known — build a standing brief for
   *  the current frontier). Emits a MemoryUsage record tagged with `phase`. */
  private async assemble(userPrompt: string | null, phase: "warm" | "construct", session?: string): Promise<ConstructedPrompt> {
    const recall = await recallContext(this.memory, userPrompt ?? this.recentQuery(), {
      entity: this.opts.entity,
      session,
      topK: this.opts.topK,
    });
    const parts = {
      standards: this.opts.standards.trim(),
      longTerm: recall.block.trim(),
      shortTerm: this.renderShortTerm(),
      userPrompt: userPrompt?.trim() ?? "(next turn not yet known — build the standing brief for the current task frontier)",
    };
    const input = [
      section("STANDARDS", parts.standards || "(none)"),
      section("LONG-TERM MEMORY", parts.longTerm || "(none recalled)"),
      section("RECENT CONTEXT (most recent last)", parts.shortTerm || "(none)"),
      section("USER PROMPT", parts.userPrompt),
    ].join("\n\n");

    const { text, usage } = await callChat(MEMORY_ROTOR_SYSTEM, input, this.model);
    const record: MemoryUsage = {
      service: "memory-rotor",
      phase,
      model: this.model,
      inputTokens: usage.inputTokens,
      outputTokens: usage.outputTokens,
      sessionId: this.sessionId,
    };
    this.onUsage?.(record);
    return { systemPrompt: text.trim(), parts, usage: record };
  }

  /** On-demand (cold) construction — a full assembler call in the critical path.
   *  The live loop avoids this via warm()/next(); it's the fallback when nothing
   *  has been warmed yet (e.g. the very first turn). */
  async construct(userPrompt: string, ctx: { session?: string } = {}): Promise<ConstructedPrompt> {
    return this.assemble(userPrompt, "construct", ctx.session);
  }

  /**
   * FOREGROUND, no LLM generation. Return the worker's system prompt for this
   * turn: the cached warm brief + a deterministic recall delta for the actual
   * prompt. The caller passes the user prompt to the worker verbatim, separately.
   * `warm=true` means it was served free from cache; `warm=false` means nothing
   * was warmed yet and the user waited on a cold assemble (first turn only).
   */
  async next(userPrompt: string, ctx: { session?: string } = {}): Promise<{ systemPrompt: string; warm: boolean; recallDelta: string; usage?: MemoryUsage }> {
    this.buffer.push({ role: "user", text: userPrompt });
    if (!this.warmBrief) {
      const cold = await this.assemble(userPrompt, "construct", ctx.session);
      return { systemPrompt: cold.systemPrompt, warm: false, recallDelta: "", usage: cold.usage };
    }
    // Deterministic recall only — the sole live wire in the foreground path.
    const recall = await recallContext(this.memory, userPrompt, {
      entity: this.opts.entity,
      session: ctx.session,
      topK: this.opts.topK,
    });
    const delta = recall.block.trim();
    const systemPrompt = [this.warmBrief, delta && section("Also relevant this turn", delta)].filter(Boolean).join("\n\n");
    return { systemPrompt, warm: true, recallDelta: delta };
  }

  /**
   * BACK PATH, after the worker responds. Observe the exchange (distil the last
   * user turn to facts) and pre-build + cache the NEXT turn's brief. Off the
   * user's clock — this is where the assembler burn happens. Returns the usage.
   */
  async warm(assistantText: string, ctx: { session?: string } = {}): Promise<MemoryUsage> {
    if (assistantText.trim()) this.buffer.push({ role: "assistant", text: assistantText });
    await this.distillLastUserTurn(ctx.session);
    const brief = await this.assemble(null, "warm", ctx.session);
    this.warmBrief = brief.systemPrompt;
    return brief.usage;
  }

  /** The most recent user turn's text — the query proxy for warm-time recall. */
  private recentQuery(): string {
    for (let i = this.buffer.length - 1; i >= 0; i--) if (this.buffer[i]!.role === "user") return this.buffer[i]!.text;
    return this.buffer.at(-1)?.text ?? "";
  }

  /** Distil the most recent user turn into facts (back-path schema-on-write). */
  private async distillLastUserTurn(session?: string): Promise<void> {
    for (let i = this.buffer.length - 1; i >= 0; i--) {
      if (this.buffer[i]!.role === "user") {
        await this.distill(this.buffer[i]!.text, session);
        return;
      }
    }
  }
}

function section(title: string, body: string): string {
  return `### ${title}\n${body}`;
}

/**
 * The ATTENTION loop's read on the conversation. It sees ONLY the thread (prompts +
 * responses) — never memory — and keeps TRUE NORTH: the one goal, plus whether the
 * latest exchange is serving it. This is the "am I going down a rabbit hole?" monitor,
 * separate from recall (which retrieves) — attention is the *derivative* (toward/away).
 */
export interface AttentionState {
  /** The session's ONE overall goal, refined — kept stable across tangents. */
  trueNorth: string;
  /** Is the latest exchange serving true north? */
  onTrack: boolean;
  /** If off-track, what pulled us off (else ""). */
  drift: string;
  /** Toward true north: "advancing" | "stuck" | "circling". */
  progress: string;
  /** If we've gone down a rabbit hole, name it (else ""). */
  rabbitHole: string;
}

/** The attention loop's system prompt — a narrow monitor, conversation only. */
export const ATTENTION_SYSTEM = [
  "You are ATTENTION — the part of a mind that holds TRUE NORTH and notices when it's",
  "going down a rabbit hole. You watch ONLY the conversation: the user's prompts and",
  "the assistant's responses. You do NOT see memory, files, or facts — just the thread.",
  "",
  "Given the RECENT CONVERSATION and the PRIOR TRUE NORTH (may be empty), output ONLY",
  "this JSON (no prose, no fence):",
  '{"trueNorth": string, "onTrack": boolean, "drift": string, "progress": string, "rabbitHole": string}',
  "  - trueNorth: the ONE overall goal this session serves, refined.",
  "  - onTrack: is the LATEST exchange serving true north?",
  '  - drift: if not on track, what pulled us off (else "").',
  '  - progress: "advancing" | "stuck" | "circling" — toward true north.',
  '  - rabbitHole: if we\'ve gone down one, name it (else "").',
  "",
  "Keep true north STABLE: refine it, don't let it wander with every tangent. A tangent",
  "is DRIFT, not a new goal — only a clear, deliberate pivot changes true north.",
].join("\n");

/**
 * Run the ATTENTION loop: watch the conversation, keep true north, flag drift /
 * rabbit-holes. Sees ONLY `recent` (prompts + responses) + the prior true north.
 * Cheap by design (narrow input). Best-effort — on any failure it keeps the prior
 * true north and reports on-track (attention must never break a turn).
 */
export async function attentionCheck(
  recent: string,
  priorTrueNorth: string,
  model: string,
  opts: { gatewayUrl?: string; gatewayToken?: string; runId?: string; onUsage?: (u: MemoryUsage) => void } = {},
): Promise<AttentionState> {
  const fallback: AttentionState = { trueNorth: priorTrueNorth, onTrack: true, drift: "", progress: "advancing", rabbitHole: "" };
  try {
    const input = [
      section("PRIOR TRUE NORTH", priorTrueNorth.trim() || "(none yet — establish it)"),
      section("RECENT CONVERSATION (prompts + responses, most recent last)", recent.trim() || "(none)"),
    ].join("\n\n");
    const { text, usage } =
      opts.gatewayUrl && opts.gatewayToken
        ? await callGatewayAssembler(opts.gatewayUrl, opts.gatewayToken, opts.runId, model, ATTENTION_SYSTEM, input)
        : await callChat(ATTENTION_SYSTEM, input, model);
    opts.onUsage?.({ service: "memory-rotor", phase: "warm", model, inputTokens: usage.inputTokens, outputTokens: usage.outputTokens });
    const json = JSON.parse(text.replace(/^[^{]*/, "").replace(/[^}]*$/, "")) as Partial<AttentionState>;
    return {
      trueNorth: (json.trueNorth || priorTrueNorth || "").trim(),
      onTrack: json.onTrack !== false,
      drift: (json.drift ?? "").trim(),
      progress: (json.progress ?? "advancing").trim(),
      rabbitHole: (json.rabbitHole ?? "").trim(),
    };
  } catch {
    return fallback;
  }
}

/** Render an AttentionState into the FOCUS text the assembler leads with. */
export function renderFocus(a: AttentionState): string {
  const parts = [`True north: ${a.trueNorth || "(establishing)"}`];
  parts.push(a.onTrack ? `On track (${a.progress}).` : `DRIFTING — ${a.drift || "off true north"}. Refocus on true north.`);
  if (a.rabbitHole) parts.push(`RABBIT HOLE: ${a.rabbitHole}. Pull back to true north before continuing.`);
  return parts.join(" ");
}

export interface ConstructInput {
  userPrompt: string;
  /** The assembler model id (an org-supported catalog model). */
  model: string;
  /** The session's PRIMARY FOCUS / overall goal (the attention anchor). Empty ⇒ the
   *  assembler infers it from `recent` + `userPrompt`. */
  goal?: string;
  /** The long-term memory the host already recalled (rrotor's stator block). */
  longTerm?: string;
  /** Cached mid-tier standards/rules text. */
  standards?: string;
  /** Recent raw turns, most-recent last. */
  recent?: string;
  /** The rotor's base system prompt to preserve above the constructed brief. */
  baseSystem?: string;
  sessionId?: string;
  /** Route the assembler through the metered gateway (Anthropic-compatible) so its
   *  tokens are billed by the model's rate card; else it calls the configured host. */
  gatewayUrl?: string;
  gatewayToken?: string;
  runId?: string;
  onUsage?: (u: MemoryUsage) => void;
}

/**
 * Build a worker system prompt from ALREADY-RECALLED inputs (no MemoryPlugin) —
 * the integration seam for a host that fetches memory its own way (rrotor's
 * runHarness recalls via the stator API, not a direct plugin). Routes the
 * assembler through the metered gateway when given. Emits a MemoryUsage record.
 */
export async function constructSystemPrompt(inp: ConstructInput): Promise<{ systemPrompt: string; usage: MemoryUsage }> {
  const input = [
    section("PRIMARY FOCUS (the goal)", (inp.goal ?? "").trim() || "(none given — infer it from RECENT CONTEXT + USER PROMPT)"),
    section("STANDARDS", (inp.standards ?? "").trim() || "(none)"),
    section("LONG-TERM MEMORY", (inp.longTerm ?? "").trim() || "(none recalled)"),
    section("RECENT CONTEXT (prompts + responses, most recent last)", (inp.recent ?? "").trim() || "(none)"),
    section("USER PROMPT", inp.userPrompt.trim()),
  ].join("\n\n");

  const { text, usage } =
    inp.gatewayUrl && inp.gatewayToken
      ? await callGatewayAssembler(inp.gatewayUrl, inp.gatewayToken, inp.runId, inp.model, MEMORY_ROTOR_SYSTEM, input)
      : await callChat(MEMORY_ROTOR_SYSTEM, input, inp.model);

  const built = text.trim();
  const systemPrompt = inp.baseSystem?.trim() ? `${inp.baseSystem.trim()}\n\n${built}` : built;
  const record: MemoryUsage = {
    service: "memory-rotor",
    phase: "construct",
    model: inp.model,
    inputTokens: usage.inputTokens,
    outputTokens: usage.outputTokens,
    sessionId: inp.sessionId,
  };
  inp.onUsage?.(record);
  return { systemPrompt, usage: record };
}

/** Anthropic Messages against the metered gateway (x-api-key = the run's token,
 *  x-glyphh-run correlates the spend). The gateway prices it by the model's rate. */
async function callGatewayAssembler(
  gatewayUrl: string, token: string, runId: string | undefined, model: string,
  system: string, user: string, env: NodeJS.ProcessEnv = process.env,
): Promise<ChatResult> {
  const base = gatewayUrl.replace(/\/+$/, "");
  const url = base.endsWith("/v1") ? `${base}/messages` : `${base}/v1/messages`;
  const maxTokens = Number(env.MEMORY_ROTOR_MAX_TOKENS) || 1200;
  const timeoutMs = Number(env.MEMORY_ROTOR_TIMEOUT) || 30_000;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  (timer as { unref?: () => void }).unref?.();
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-api-key": token,
        "anthropic-version": "2023-06-01",
        ...(runId ? { "x-glyphh-run": runId } : {}),
      },
      body: JSON.stringify({ model, max_tokens: maxTokens, system, messages: [{ role: "user", content: user }] }),
      signal: ctrl.signal,
    });
    if (!res.ok) throw new Error(`memory rotor: gateway ${res.status}: ${(await res.text().catch(() => "")).slice(0, 300)}`);
    const json = (await res.json()) as { content?: Array<{ type: string; text?: string }>; usage?: { input_tokens?: number; output_tokens?: number } };
    const text = (json.content ?? []).filter((b) => b.type === "text" && b.text).map((b) => b.text as string).join("");
    return { text, usage: { inputTokens: json.usage?.input_tokens ?? 0, outputTokens: json.usage?.output_tokens ?? 0 } };
  } finally {
    clearTimeout(timer);
  }
}

interface ChatResult {
  text: string;
  usage: { inputTokens: number; outputTokens: number };
}

/**
 * Call the small assembler model. Anthropic Messages by default (Haiku); an
 * OpenAI-compatible base is used when MEMORY_ROTOR_MODEL_URL points elsewhere.
 * Self-contained transport — no SDK — mirroring the enricher.
 */
async function callChat(system: string, user: string, model: string, env: NodeJS.ProcessEnv = process.env): Promise<ChatResult> {
  const base = (env.MEMORY_ROTOR_MODEL_URL ?? "https://api.anthropic.com").trim().replace(/\/+$/, "");
  const maxTokens = Number(env.MEMORY_ROTOR_MAX_TOKENS) || 1200;
  const timeoutMs = Number(env.MEMORY_ROTOR_TIMEOUT) || 30_000;
  const anthropic = /anthropic\.com/i.test(base) || /^claude/i.test(model);

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  (timer as { unref?: () => void }).unref?.();
  try {
    return anthropic
      ? await callAnthropic(base, model, system, user, maxTokens, env, ctrl.signal)
      : await callOpenAI(base, model, system, user, maxTokens, env, ctrl.signal);
  } finally {
    clearTimeout(timer);
  }
}

async function callAnthropic(
  base: string, model: string, system: string, user: string, maxTokens: number,
  env: NodeJS.ProcessEnv, signal: AbortSignal,
): Promise<ChatResult> {
  const key = (env.MEMORY_ROTOR_API_KEY ?? env.ANTHROPIC_API_KEY ?? "").trim();
  if (!key) throw new Error("memory rotor: ANTHROPIC_API_KEY (or MEMORY_ROTOR_API_KEY) is not set");
  const url = base.endsWith("/v1") ? `${base}/messages` : `${base}/v1/messages`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01" },
    body: JSON.stringify({ model, max_tokens: maxTokens, system, messages: [{ role: "user", content: user }] }),
    signal,
  });
  if (!res.ok) throw new Error(`memory rotor: Anthropic ${res.status}: ${(await res.text().catch(() => "")).slice(0, 300)}`);
  const json = (await res.json()) as { content?: Array<{ type: string; text?: string }>; usage?: { input_tokens?: number; output_tokens?: number } };
  const text = (json.content ?? []).filter((b) => b.type === "text" && b.text).map((b) => b.text as string).join("");
  return { text, usage: { inputTokens: json.usage?.input_tokens ?? 0, outputTokens: json.usage?.output_tokens ?? 0 } };
}

async function callOpenAI(
  base: string, model: string, system: string, user: string, maxTokens: number,
  env: NodeJS.ProcessEnv, signal: AbortSignal,
): Promise<ChatResult> {
  const key = (env.MEMORY_ROTOR_API_KEY ?? "").trim();
  const res = await fetch(`${base}/chat/completions`, {
    method: "POST",
    headers: { "content-type": "application/json", ...(key ? { authorization: `Bearer ${key}` } : {}) },
    body: JSON.stringify({
      model, max_tokens: maxTokens, temperature: 0,
      messages: [{ role: "system", content: system }, { role: "user", content: user }],
    }),
    signal,
  });
  if (!res.ok) throw new Error(`memory rotor: OpenAI ${res.status}: ${(await res.text().catch(() => "")).slice(0, 300)}`);
  const json = (await res.json()) as { choices?: Array<{ message?: { content?: string } }>; usage?: { prompt_tokens?: number; completion_tokens?: number } };
  return {
    text: json.choices?.[0]?.message?.content ?? "",
    usage: { inputTokens: json.usage?.prompt_tokens ?? 0, outputTokens: json.usage?.completion_tokens ?? 0 },
  };
}
