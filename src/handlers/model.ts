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

export const modelHandler: StepHandler = {
  type: "model",
  async execute({ step, input, env, plugins }: HandlerArgs): Promise<StepResult> {
    const cfg = (step.config ?? {}) as ModelConfig;
    const promptText = String(input.text ?? input.prompt ?? "");

    let candidates: string[] | undefined;
    if (cfg.ground) {
      const entity = String(input.entity ?? cfg.ground.entity ?? "");
      const role = cfg.ground.role ?? "";
      if (entity && role) {
        candidates = plugins.grounding.groundedFillers(entity, role, env.space_id);
      }
    }

    const lane = cfg.lane ?? "local";
    const res = await plugins.models.execute(
      { prompt: promptText, candidates, lane, model: cfg.model, temperature: cfg.temperature, seed: cfg.seed },
      lane,
    );
    plugins.gateway.recordUsage(res.usage, lane);
    plugins.governance.recordUsage(res.usage);

    const frames: Frame[] = res.frames.length > 0 ? res.frames : [{ type: "done" }];
    return {
      output: { text: res.text, frames: res.frames, usage: res.usage },
      frames,
      status: "ok",
      usage: res.usage,
    };
  },
};
