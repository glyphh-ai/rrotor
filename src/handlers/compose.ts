/**
 * Pure control-plane composers: `prompt` (§7.1) and `transform` (§7.17). No
 * model call, no side effect — deterministic string/state reshaping.
 */

import type { Frame, PromptConfig, StepResult, TransformConfig } from "../types.js";
import { resolveRef, tokenish } from "../exec/util.js";
import type { HandlerArgs, StepHandler } from "./types.js";

/** Interpolate `{{name}}` occurrences from the resolved `in` values. */
function interpolate(text: string, input: Record<string, unknown>): string {
  return text.replace(/\{\{\s*([\w.]+)\s*\}\}/g, (_m, name: string) => {
    const v = input[name];
    if (v === undefined || v === null) return "";
    return typeof v === "string" ? v : JSON.stringify(v);
  });
}

export const promptHandler: StepHandler = {
  type: "prompt",
  async execute({ step, input }: HandlerArgs): Promise<StepResult> {
    const cfg = (step.config ?? {}) as PromptConfig;
    let text: string;
    if (cfg.blocks && cfg.blocks.length > 0) {
      text = cfg.blocks.map((b) => interpolate(b.text, input)).join("\n");
    } else if (cfg.template) {
      text = interpolate(cfg.template, input);
    } else {
      // No template/blocks: concatenate the resolved inputs by sorted key.
      text = Object.keys(input)
        .sort()
        .map((k) => (typeof input[k] === "string" ? (input[k] as string) : JSON.stringify(input[k])))
        .join("\n");
    }
    if (cfg.max_tokens && tokenish(text) > cfg.max_tokens) {
      text = text.slice(0, cfg.max_tokens * 4);
    }
    const frames: Frame[] = [{ type: "done", data: { breakpoints: cfg.cache?.breakpoints } }];
    return { output: { text }, frames, status: "ok" };
  },
};

export const transformHandler: StepHandler = {
  type: "transform",
  async execute({ step, env }: HandlerArgs): Promise<StepResult> {
    const cfg = (step.config ?? {}) as TransformConfig;
    const output: Record<string, unknown> = {};
    // Constant injections first, then reference remaps (map wins on conflict).
    if (cfg.set) for (const k of Object.keys(cfg.set).sort()) output[k] = cfg.set[k];
    if (cfg.map) {
      for (const name of Object.keys(cfg.map).sort()) {
        output[name] = resolveRef(cfg.map[name], env.context);
      }
    }
    return { output, frames: [{ type: "done" }], status: "ok" };
  },
};
