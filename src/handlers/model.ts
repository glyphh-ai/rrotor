/**
 * `model` (§7.2) — the quarantined stochastic Task. The handler composes plugin
 * calls: when the step declares an inline `ground`, it pulls the grounded
 * continuations for `(entity, role)` from the grounding plugin and hands them to
 * the models plugin as candidates, so the zero-model ranker (basic tier) or a
 * soft verify (with a live model) can keep the loop grounded. Tokens are
 * recorded, never reproduced; usage is metered at the gateway.
 */

import type { Frame, ModelConfig, StepResult } from "../types.js";
import type { HandlerArgs, StepHandler } from "./types.js";

/** Default HDC margin the hard grounding gate requires (§6.3). */
const GROUND_MARGIN = 0.02;

export const modelHandler: StepHandler = {
  type: "model",
  async execute({ step, input, env, plugins }: HandlerArgs): Promise<StepResult> {
    const cfg = (step.config ?? {}) as ModelConfig;
    const promptText = String(input.text ?? input.prompt ?? "");

    // Hard grounding gate (§6.3): when the step declares an inline `ground`, the
    // model may decode ONLY into the grounded fillers for (entity, role). With no
    // grounded continuation and hard enforcement, the step refuses rather than
    // inventing one.
    let candidates: string[] | undefined;
    let ground: { entity: string; role: string; hard: boolean } | undefined;
    if (cfg.ground) {
      const entity = String(input.entity ?? cfg.ground.entity ?? "");
      const role = cfg.ground.role ?? "";
      const hard = (cfg.ground.enforcement ?? "hard") === "hard";
      if (entity && role) {
        ground = { entity, role, hard };
        candidates = plugins.grounding.groundedFillers(entity, role, env.space_id);
        if (hard && candidates.length === 0) {
          return {
            output: { text: "", refused: true, reason: "E_UNGROUNDED" },
            frames: [{ type: "refuse", data: { entity, role, reason: "no grounded continuation" } }],
            status: "refused",
          };
        }
      }
    }

    const lane = cfg.lane ?? "local";
    const res = await plugins.models.execute(
      { prompt: promptText, candidates, lane, model: cfg.model, temperature: cfg.temperature, seed: cfg.seed },
      lane,
    );
    plugins.gateway.recordUsage(res.usage, lane);
    plugins.governance.recordUsage(res.usage);
    plugins.memory.recordTurn(res.text); // short-term memory: the outbound completion

    // Under hard enforcement, verify the decoded text is grounded with margin;
    // a low-margin / ungrounded output refuses (§6.3).
    if (ground?.hard) {
      const v = plugins.grounding.verify(ground.entity, ground.role, res.text, GROUND_MARGIN, env.space_id);
      if (!v.grounded) {
        return {
          output: { text: cfg.ground?.refusal ?? res.text, refused: true, reason: "E_UNGROUNDED", margin: v.margin },
          frames: [...res.frames, { type: "dispose", data: { margin: v.margin } }, { type: "refuse" }],
          status: "refused",
          usage: res.usage,
        };
      }
    }

    const frames: Frame[] = res.frames.length > 0 ? res.frames : [{ type: "done" }];
    return {
      output: { text: res.text, frames: res.frames, usage: res.usage },
      frames,
      status: "ok",
      usage: res.usage,
    };
  },
};
