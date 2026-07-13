/**
 * The control-composing handlers that run OTHER steps through the {@link Engine}
 * handle: `loop` (§7.12), `parallel` (§7.13), and `sub-rotor` (§7.19).
 *
 * - `loop` re-enters its body while the scoring gate rejects AND iterations <
 *   max — termination is guaranteed by the mandatory cap.
 * - `parallel` fans out over branches (recorded/declared order) and merges the
 *   results with the declared reducer; an undeclared fan-in defaults to `merge`.
 * - `sub-rotor` recursively executes a referenced rotor and returns its
 *   projected outputs.
 */

import type {
  LoopConfig,
  ParallelConfig,
  Reducer,
  StepResult,
  SubRotorConfig,
} from "../types.js";
import { applyReducer, resolveRef } from "../exec/util.js";
import { evalGate } from "./gate.js";
import type { HandlerArgs, StepHandler } from "./types.js";

export const loopHandler: StepHandler = {
  type: "loop",
  async execute({ step, env, plugins, engine }: HandlerArgs): Promise<StepResult> {
    const cfg = step.config as LoopConfig;
    const max = cfg.max_iterations;
    let iterations = 0;
    let last: StepResult | undefined;
    let accepted = false;

    while (iterations < max) {
      last = await engine.runStep(cfg.body);
      iterations++;
      if (!cfg.gate) {
        accepted = true;
        break;
      }
      const g = evalGate(cfg.gate, last.output, env, plugins);
      if (g.verdict === "pass") {
        accepted = true;
        break;
      }
    }

    const exhausted = !accepted;
    let status: StepResult["status"] = "ok";
    if (exhausted && cfg.on_exhausted === "refuse") status = "refused";
    else if (exhausted && cfg.on_exhausted === "escalate") status = "escalated";

    return {
      output: { result: last?.output ?? {}, iterations, exhausted },
      frames: [{ type: "done", data: { iterations, exhausted } }],
      status,
    };
  },
};

export const parallelHandler: StepHandler = {
  type: "parallel",
  async execute({ step, env, engine }: HandlerArgs): Promise<StepResult> {
    const cfg = step.config as ParallelConfig;
    const merged: Record<string, unknown> = {};

    const reducerFor = (key: string): Reducer => {
      if (cfg.reducer && typeof cfg.reducer === "object") return cfg.reducer[key] ?? "merge";
      if (typeof cfg.reducer === "string") return cfg.reducer;
      return "merge";
    };
    const mergeIn = (out: Record<string, unknown>): void => {
      for (const key of Object.keys(out).sort()) {
        merged[key] =
          key in merged ? applyReducer(merged[key], out[key], reducerFor(key)) : out[key];
      }
    };

    if (cfg.mode === "map" && cfg.over && cfg.body) {
      const coll = resolveRef(cfg.over, env.context);
      const items = Array.isArray(coll) ? coll : [];
      const asName = cfg.as ?? "item";
      for (let i = 0; i < items.length; i++) {
        // Bind the current item into shared state for the body to read.
        env.context.state[asName] = items[i];
        const r = await engine.runStep(cfg.body);
        mergeIn(r.output);
      }
    } else {
      // Fan-out over declared branches, deterministic order.
      for (const bid of cfg.branches ?? []) {
        const r = await engine.runStep(bid);
        mergeIn(r.output);
      }
    }

    return { output: merged, frames: [{ type: "done" }], status: "ok" };
  },
};

export const subRotorHandler: StepHandler = {
  type: "sub-rotor",
  async execute({ step, env, engine }: HandlerArgs): Promise<StepResult> {
    const cfg = step.config as SubRotorConfig;
    const mapped: Record<string, unknown> = {};
    if (cfg.inputs) {
      for (const name of Object.keys(cfg.inputs).sort()) {
        mapped[name] = resolveRef(cfg.inputs[name], env.context);
      }
    }
    const rr = await engine.runRotor(cfg.ref, mapped);
    return {
      output: rr.outputs,
      frames: [{ type: "done", data: { ref: cfg.ref, status: rr.status } }],
      status: rr.status === "failed" ? "failed" : "ok",
    };
  },
};
