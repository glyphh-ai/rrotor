/**
 * The effectful boundary handlers that cross the gateway/connections seams:
 * `escalate` (§7.15 — the local → frontier → human ladder) and `tool` (§7.16 —
 * `tool.mcp` substrate tools / `tool.app` client methods over the loopback
 * registry). Both are recorded + idempotent; a `FrontierDeclined` on escalate
 * falls back to a local lane rather than crashing.
 */

import type { EscalateConfig, Frame, StepResult, ToolConfig } from "../types.js";
import type { HandlerArgs, StepHandler } from "./types.js";

export const escalateHandler: StepHandler = {
  type: "escalate",
  async execute({ step, input, plugins }: HandlerArgs): Promise<StepResult> {
    const cfg = (step.config ?? { trigger: "refuse" }) as EscalateConfig;
    const rung = plugins.models.escalate(cfg.trigger, cfg.to, cfg.fallback);

    if (rung.lane === "human") {
      // The human rung surfaces the refusal; no model call is made.
      return {
        output: { lane: "human", text: String(input.text ?? ""), usage: { cost: 0 } },
        frames: [{ type: "refuse", data: { escalated: "human" } }],
        status: "escalated",
      };
    }

    const promptText = String(input.text ?? input.prompt ?? "");
    const res = await plugins.models.execute(
      { prompt: promptText, lane: rung.lane, model: cfg.frontier_model },
      rung.lane,
    );
    plugins.gateway.recordUsage(res.usage, rung.lane);
    plugins.governance.recordUsage(res.usage);
    const frames: Frame[] = [{ type: "tool", data: { lane: rung.lane } }, ...res.frames];
    return {
      output: { lane: rung.lane, text: res.text, usage: res.usage },
      frames,
      status: "escalated",
      usage: res.usage,
    };
  },
};

export const toolHandler: StepHandler = {
  type: "tool",
  async execute({ step, input, plugins }: HandlerArgs): Promise<StepResult> {
    const cfg = (step.config ?? { flavor: "mcp" }) as ToolConfig;
    const method = cfg.method ?? cfg.name ?? "";
    const args = { ...(cfg.args ?? {}), ...(cfg.params ?? {}), ...input };
    const res = await plugins.connections.dispatch(method, args);
    if (res.ok) {
      return {
        output: { result: res.result },
        frames: [{ type: "tool", data: { method, flavor: cfg.flavor } }, { type: "done" }],
        status: "ok",
      };
    }
    return {
      output: { error: res.error },
      frames: [{ type: "tool", data: { method, error: res.error } }],
      status: "failed",
      error: { name: "E_TOOL", cause: res.error },
    };
  },
};
