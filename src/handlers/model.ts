/**
 * `model` (§7.2) — the quarantined stochastic Task. The handler composes plugin
 * calls: it pulls grounded continuations for an inline `ground`, optionally runs
 * the word-level **micro rotor** (propose → dispose → backtrack, §6.3) bounded by
 * `max_backtracks`, enforces the hard grounding gate, and lowers prompt-cache
 * breakpoints to the gateway (§8.6). Tokens are recorded, never reproduced; usage
 * (including cache read/write) is metered at the gateway.
 */

import type { Frame, ModelConfig, StepResult, Usage } from "../types.js";
import { sha256, tokenish } from "../exec/util.js";
import type { HandlerArgs, StepHandler } from "./types.js";

/** Default HDC margin the hard grounding gate requires (§6.3). */
const GROUND_MARGIN = 0.02;
const norm = (s: unknown): string => String(s ?? "").trim().toLowerCase();

export const modelHandler: StepHandler = {
  type: "model",
  async execute({ step, input, env, plugins }: HandlerArgs): Promise<StepResult> {
    const cfg = (step.config ?? {}) as ModelConfig;
    const promptText = String(input.text ?? input.prompt ?? "");
    const lane = cfg.lane ?? "local";
    const provider = lane === "frontier" ? "anthropic" : "local";

    // Grounded continuations for an inline ground (the hard-gate mask, §6.3).
    let candidates: string[] | undefined;
    let ground: { entity: string; role: string; hard: boolean } | undefined;
    if (cfg.ground) {
      const entity = String(input.entity ?? cfg.ground.entity ?? "");
      const role = cfg.ground.role ?? "";
      const hard = (cfg.ground.enforcement ?? "hard") === "hard";
      if (entity && role) {
        ground = { entity, role, hard };
        candidates = await plugins.grounding.groundedFillers(entity, role, env.space_id);
        if (hard && candidates.length === 0) {
          return {
            output: { text: "", refused: true, reason: "E_UNGROUNDED" },
            frames: [{ type: "refuse", data: { entity, role, reason: "no grounded continuation" } }],
            status: "refused",
          };
        }
      }
    }

    // ── decode: micro rotor, or a single model call ──────────────────────────
    let text: string;
    let usage: Usage;
    let served: string | undefined;
    let laneNotes: string[] | undefined;
    let decodeFrames: Frame[];

    if (cfg.micro && ground && candidates && candidates.length > 0) {
      // Decode over the role's ranked vocabulary (distractors give the backtrack
      // loop something to reject); the grounded winner is the probe's top filler.
      const p = await plugins.grounding.probe(ground.entity, ground.role, env.space_id);
      const vocab = p.top.length > 0 ? p.top : candidates;
      const micro = runMicroRotor(vocab, p.filler, cfg.max_backtracks ?? vocab.length);
      decodeFrames = micro.frames;
      if (micro.chosen === undefined) {
        return {
          output: { text: "", refused: true, reason: "E_UNGROUNDED", backtracks: micro.backtracks },
          frames: [...micro.frames, { type: "refuse", data: { reason: "backtracks exhausted" } }],
          status: "refused",
        };
      }
      text = micro.chosen;
      usage = { input: tokenish(promptText), output: tokenish(text), cost: 0 };
      served = "hdc";
    } else {
      const res = await plugins.models.execute(
        { prompt: promptText, candidates, lane, model: cfg.model, temperature: cfg.temperature, seed: cfg.seed, timeout_ms: cfg.timeout_ms, ...(env.signal ? { signal: env.signal } : {}) },
        lane,
      );
      text = res.text;
      usage = res.usage;
      decodeFrames = res.frames;
      served = res.served;
      laneNotes = res.notes;
    }

    // ── prompt caching (§8.6) — determinism-neutral; changes only usage ──────
    const cacheFrames: Frame[] = [];
    if (cfg.cache?.breakpoints && cfg.cache.breakpoints.length > 0) {
      plugins.gateway.lowerPromptCache(cfg.cache.breakpoints, provider);
      const prefixKey = sha256(provider, ":", cfg.model ?? "", ":", cfg.cache.breakpoints.slice().sort().join(","));
      const pc = plugins.gateway.accountPromptCache(prefixKey, usage.input ?? tokenish(promptText));
      usage = { ...usage, cache_read: pc.cache_read, cache_write: pc.cache_write };
      cacheFrames.push({ type: "cache", disposition: pc.disposition, logical_tick: env.logical_tick });
    }

    plugins.gateway.recordUsage(usage, lane);
    plugins.governance.recordUsage(usage);
    plugins.memory.recordTurn(text); // short-term memory: the outbound completion

    // Hard grounding gate on the decoded output (§6.3).
    if (ground?.hard) {
      const v = await plugins.grounding.verify(ground.entity, ground.role, text, GROUND_MARGIN, env.space_id);
      if (!v.grounded) {
        return {
          output: { text: cfg.ground?.refusal ?? text, refused: true, reason: "E_UNGROUNDED", margin: v.margin },
          frames: [...decodeFrames, ...cacheFrames, { type: "dispose", data: { margin: v.margin } }, { type: "refuse" }],
          status: "refused",
          usage,
        };
      }
    }

    const frames: Frame[] = [...decodeFrames, ...cacheFrames];
    return {
      output: {
        text,
        usage,
        ...(served ? { served } : {}),
        ...(laneNotes?.length ? { lane_notes: laneNotes } : {}),
      },
      frames: frames.length > 0 ? frames : [{ type: "done" }],
      status: "ok",
      usage,
    };
  },
};

/**
 * The word-level micro rotor (§6.3, basic tier): propose the grounded candidates
 * in sorted order, backtracking past non-winners until the grounded winner is
 * reached — bounded by `maxBacktracks`. Deterministic; emits the
 * propose → dispose → backtrack → assert frame stream.
 */
function runMicroRotor(
  candidates: string[],
  winner: string | null,
  maxBacktracks: number,
): { chosen?: string; backtracks: number; frames: Frame[] } {
  const sorted = [...candidates].sort();
  const target = winner ?? sorted[0];
  const frames: Frame[] = [];
  let backtracks = 0;
  for (const c of sorted) {
    frames.push({ type: "propose", data: { candidate: c } });
    if (norm(c) === norm(target)) {
      frames.push({ type: "assert", data: { candidate: c } });
      return { chosen: c, backtracks, frames };
    }
    frames.push({ type: "dispose", data: { candidate: c } }, { type: "backtrack" });
    backtracks++;
    if (backtracks > maxBacktracks) break;
  }
  return { chosen: undefined, backtracks, frames };
}
