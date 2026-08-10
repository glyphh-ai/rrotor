/**
 * harness/enricher.ts — the LLM fact enricher (the premium schema-on-write).
 *
 * The deterministic `absorbText` is the reliable floor: regex over sentence
 * shapes, no coreference, indirect phrasing falls to `raw.text`. This enricher
 * is the upgrade: a small local model (qwen3-14b via an OpenAI-compatible host —
 * the /runtime project's Ollama pattern, `ROTOR_LOCAL_MODEL_URL`) reads the turn
 * and emits structured `(entity, role, filler, tier)` facts.
 *
 * Determinism-by-recording (docs/memory.md mechanism 3): the model runs ONCE at
 * write time and its extraction is persisted as the facts themselves — recall
 * never re-invokes it. Best-effort by construction: no endpoint, a timeout, or
 * unparseable output all fall back to `absorbText`; a memory write never fails
 * a turn on the enricher.
 *
 * Config:
 *   ROTOR_ENRICH_MODEL_URL — model base. Anthropic (…anthropic.com, or a claude-*
 *                            model id) uses the Messages API; anything else is
 *                            OpenAI-compatible /chat/completions. Falls back to
 *                            ROTOR_LOCAL_MODEL_URL (e.g. http://models:11434/v1).
 *   ROTOR_ENRICH_MODEL_ID  — model id (default `qwen3:14b`). A `claude-*` id routes
 *                            to the Anthropic branch (premium extraction).
 *   ROTOR_ENRICH_API_KEY   — bearer for the enrich endpoint (Anthropic: falls back
 *                            to ANTHROPIC_API_KEY).
 *   ROTOR_ENRICH_TIMEOUT   — ms budget for the extraction call (default 20000)
 */

import { log } from "../obs/logger.js";

export interface EnrichedFact {
  entity: string;
  role: string;
  filler: string;
  tier?: "short" | "mid" | "long";
  key?: string;
}

// Small models need WORKED EXAMPLES, not rules — the few-shot pairs below are
// what makes a 1.7b produce whole-value fillers and catch directives.
const SYSTEM = [
  "You extract memory facts about the USER from one message they wrote.",
  "Return ONLY a JSON array (no prose, no fence). Each element:",
  '{"entity": string, "role": string, "filler": string, "tier": "short"|"mid"|"long"}',
  "Rules:",
  '- entity is "user" unless the fact is clearly about a named other person/thing.',
  "- role: one snake_case slot — name, preference, directive, project, deadline, decision, location, tool…",
  "- filler: the COMPLETE value as a readable phrase, taken from the text — never a fragment, never invented.",
  '- role "directive" is ONLY a standing instruction about how the ASSISTANT should behave',
  '  (always/never/from now on/sign as/reply in…), tier "long". Something the USER is doing',
  '  or planning is a task/decision/project — never a directive.',
  '- Identity + durable preferences → "long". Task/project facts → "mid". Session-only detail → "short".',
  "- No facts worth keeping → []",
  "",
  'Example — message: "My name is Ada and I always want replies in Spanish. The retro is Thursday."',
  'Output: [{"entity":"user","role":"name","filler":"Ada","tier":"long"},',
  '{"entity":"user","role":"directive","filler":"always reply in Spanish","tier":"long"},',
  '{"entity":"user","role":"deadline","filler":"the retro is Thursday","tier":"short"}]',
  "",
  'Example — message: "We decided to move the billing service to Postgres; ping Sam about the keys."',
  'Output: [{"entity":"billing service","role":"decision","filler":"move to Postgres","tier":"mid"},',
  '{"entity":"user","role":"task","filler":"ping Sam about the keys","tier":"short"}]',
  "",
  // qwen3 soft switch: skip the <think> pass — extraction needs speed, not
  // deliberation. Harmless noise to models that don't know it.
  "/no_think",
].join("\n");

/** Extract facts from `text` with the configured local model. Returns null when
 *  the enricher is unconfigured or fails — the caller falls back to absorbText. */
export async function enrichFacts(text: string, entity: string, env: NodeJS.ProcessEnv = process.env): Promise<EnrichedFact[] | null> {
  const base = (env.ROTOR_ENRICH_MODEL_URL ?? env.ROTOR_LOCAL_MODEL_URL ?? "").trim().replace(/\/+$/, "");
  if (!base || !text.trim()) return null;
  const model = env.ROTOR_ENRICH_MODEL_ID ?? "qwen3:14b";
  const timeoutMs = Number(env.ROTOR_ENRICH_TIMEOUT) || 20_000;
  const input = text.slice(0, 8000);
  const anthropic = /anthropic\.com/i.test(base) || /^claude/i.test(model);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  (timer as { unref?: () => void }).unref?.();
  try {
    const raw = anthropic
      ? await callAnthropic(base, model, input, env, ctrl.signal)
      : await callOpenAI(base, model, input, env, ctrl.signal);
    return raw === null ? null : parseFacts(raw, entity);
  } catch (err) {
    log.debug("enricher declined", { detail: (err as Error).message });
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/** OpenAI-compatible /chat/completions (local qwen/Ollama/vLLM, or a hosted
 *  provider like Mistral). ROTOR_ENRICH_API_KEY adds Bearer auth when the endpoint
 *  needs it; a keyless local host works without it. */
async function callOpenAI(base: string, model: string, input: string, env: NodeJS.ProcessEnv, signal: AbortSignal): Promise<string | null> {
  const key = (env.ROTOR_ENRICH_API_KEY ?? "").trim();
  const res = await fetch(`${base}/chat/completions`, {
    method: "POST",
    headers: { "content-type": "application/json", ...(key ? { authorization: `Bearer ${key}` } : {}) },
    signal,
    body: JSON.stringify({
      model,
      temperature: 0,
      max_tokens: 800,
      messages: [
        { role: "system", content: SYSTEM },
        { role: "user", content: input },
      ],
    }),
  });
  if (!res.ok) return null;
  const data = (await res.json()) as { choices?: Array<{ message?: { content?: string } }> };
  return data.choices?.[0]?.message?.content ?? "";
}

/** Anthropic Messages API — the premium extraction tier (e.g. claude-haiku-4-5). */
async function callAnthropic(base: string, model: string, input: string, env: NodeJS.ProcessEnv, signal: AbortSignal): Promise<string | null> {
  const key = (env.ROTOR_ENRICH_API_KEY ?? env.ANTHROPIC_API_KEY ?? "").trim();
  if (!key) return null;
  const url = base.endsWith("/v1") ? `${base}/messages` : `${base}/v1/messages`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01" },
    signal,
    body: JSON.stringify({
      model,
      max_tokens: 800,
      temperature: 0,
      system: SYSTEM,
      messages: [{ role: "user", content: input }],
    }),
  });
  if (!res.ok) return null;
  const data = (await res.json()) as { content?: Array<{ type?: string; text?: string }> };
  return (data.content ?? []).filter((b) => b.type === "text").map((b) => b.text ?? "").join("");
}

/** Parse + validate the model's output into facts. Null on anything malformed —
 *  a hallucinated shape must fall back to the deterministic floor, not persist. */
export function parseFacts(raw: string, defaultEntity: string): EnrichedFact[] | null {
  // Reasoning models (qwen3) may prefix a <think> block whose prose can contain
  // brackets — strip it (closed or unterminated) before hunting for the array.
  const cleaned = raw.replace(/<think>[\s\S]*?(<\/think>|$)/gi, "");
  // Tolerate a fenced block or leading prose around the array.
  const m = /\[[\s\S]*\]/.exec(cleaned);
  if (!m) return null;
  let arr: unknown;
  try { arr = JSON.parse(m[0]); } catch { return null; }
  if (!Array.isArray(arr)) return null;
  const out: EnrichedFact[] = [];
  for (const f of arr.slice(0, 24)) {
    if (typeof f !== "object" || f === null) continue;
    const o = f as Record<string, unknown>;
    const entity = (typeof o.entity === "string" && o.entity.trim() ? o.entity.trim() : defaultEntity).slice(0, 64);
    const role = typeof o.role === "string" ? o.role.trim().toLowerCase().replace(/\s+/g, "_").slice(0, 64) : "";
    const filler = typeof o.filler === "string" ? o.filler.trim().slice(0, 500) : "";
    if (!role || !filler) continue;
    const tier = o.tier === "short" || o.tier === "mid" || o.tier === "long" ? o.tier : "mid";
    out.push({
      entity, role, filler, tier,
      // Directives key by content (restating dedupes, distinct coexist); other
      // slots key by entity:role so a new value supersedes the old (§7.4).
      key: role === "directive" ? `directive:${filler.toLowerCase()}` : `${entity.toLowerCase()}:${role}`,
    });
  }
  return out.length ? out : null;
}
