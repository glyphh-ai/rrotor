/**
 * Pure control-plane composers: `prompt` (§7.1) and `transform` (§7.17). No
 * model call, no side effect — deterministic string/state reshaping.
 */

import type { Frame, PromptConfig, StepResult, TransformConfig } from "../types.js";
import { interpolate, resolveRef, tokenish } from "../exec/util.js";
import type { HandlerArgs, StepHandler } from "./types.js";

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
    const frames: Frame[] = [{ type: "done", data: { breakpoints: cfg.cache?.breakpoints } }];
    const output: Record<string, unknown> = { text };
    if (cfg.max_tokens && tokenish(text) > cfg.max_tokens) {
      // Capping a composed prompt drops real content — never do it silently:
      // a degrade frame on the wire, a ✗ note in the transcript.
      const note = `prompt truncated: ~${tokenish(text)} tokens composed, max_tokens ${cfg.max_tokens}`;
      output.text = text.slice(0, cfg.max_tokens * 4);
      output.lane_notes = [note];
      frames.unshift({ type: "degrade", data: { notes: [note] } });
    }
    return { output, frames, status: "ok" };
  },
};

export const transformHandler: StepHandler = {
  type: "transform",
  async execute({ step, input, env }: HandlerArgs): Promise<StepResult> {
    const cfg = (step.config ?? {}) as TransformConfig;
    const output: Record<string, unknown> = {};
    // Constant injections first, then reference remaps, then parsed fields
    // (later stages win on conflict).
    if (cfg.set) for (const k of Object.keys(cfg.set).sort()) output[k] = cfg.set[k];
    if (cfg.map) {
      for (const name of Object.keys(cfg.map).sort()) {
        output[name] = resolveRef(cfg.map[name], env.context);
      }
    }
    if (cfg.strip === "fences") {
      const raw = String(input.text ?? "");
      const lines = raw.replace(/\s+$/, "").split("\n");
      if (lines[0]?.trimStart().startsWith("```")) lines.shift();
      if (lines[lines.length - 1]?.trim() === "```") lines.pop();
      output.text = lines.join("\n");
    }
    if (cfg.parse) {
      // Deterministic extraction over `in.text`: first capture group, trimmed;
      // unmatched → null (a downstream gate turns that into a refusal).
      const text = String(input.text ?? "");
      for (const name of Object.keys(cfg.parse).sort()) {
        const m = new RegExp(cfg.parse[name], "m").exec(text);
        output[name] = m?.[1] !== undefined ? m[1].trim() : null;
      }
    }
    return { output, frames: [{ type: "done" }], status: "ok" };
  },
};
