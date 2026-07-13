/**
 * Pure control terminals and routers that carry no subsystem work of their own:
 * `branch` (§7.11 — routing is done by `select_next`), `fail` (§7.20), `wait`
 * (§7.14), and the basic `plan` (§7.10).
 */

import type {
  FailConfig,
  Frame,
  PlanConfig,
  StepResult,
  WaitConfig,
} from "../types.js";
import type { HandlerArgs, StepHandler } from "./types.js";

export const branchHandler: StepHandler = {
  type: "branch",
  async execute(_args: HandlerArgs): Promise<StepResult> {
    // A branch is control-only: `select_next` evaluates the ordered predicates.
    return { output: {}, frames: [{ type: "done" }], status: "ok" };
  },
};

export const failHandler: StepHandler = {
  type: "fail",
  async execute({ step }: HandlerArgs): Promise<StepResult> {
    const cfg = (step.config ?? { error: "E_FAILED" }) as FailConfig;
    return {
      output: { error: cfg.error, cause: cfg.cause },
      frames: [{ type: "refuse", data: { error: cfg.error } }],
      status: "failed",
      error: { name: cfg.error, cause: cfg.cause },
    };
  },
};

export const waitHandler: StepHandler = {
  type: "wait",
  async execute({ step, input, env }: HandlerArgs): Promise<StepResult> {
    const cfg = (step.config ?? { on: "approval" }) as WaitConfig;
    const resume = input.__resume as { timeout?: boolean } | undefined;

    // No resume payload → pause and checkpoint the completed prefix (§7.14).
    if (!resume) {
      return {
        output: { resumed_with: null, awaiting: cfg.on, token: cfg.token, value: input.draft ?? input.value },
        frames: [{ type: "done", logical_tick: env.logical_tick, data: { awaiting: cfg.on } }],
        status: "interrupted",
      };
    }

    // Resumed. A timeout resume routes via on_timeout (select_next); otherwise the
    // run continues with the injected payload.
    if (resume.timeout) {
      return {
        output: { timedOut: true, awaiting: cfg.on },
        frames: [{ type: "done", logical_tick: env.logical_tick, data: { timedOut: true } }],
        status: "ok",
      };
    }
    return {
      output: { resumed: true, awaiting: cfg.on, value: input.value ?? input.payload ?? input.draft },
      frames: [{ type: "done", logical_tick: env.logical_tick, data: { resumed: true } }],
      status: "ok",
    };
  },
};

export const planHandler: StepHandler = {
  type: "plan",
  async execute({ step, input, env, plugins }: HandlerArgs): Promise<StepResult> {
    const cfg = (step.config ?? {}) as PlanConfig;
    const question = String(input.question ?? input.text ?? "");
    const ops = cfg.ops ?? [];
    // Typed constrained decode → the router picks an in-schema op (or refuses).
    const picked = ops.length > 0 ? plugins.models.classify(question, ops) : { class: "OUT_OF_SCHEMA", margin: 0 };
    if (picked.class === "OUT_OF_SCHEMA" && cfg.on_out_of_schema === "refuse") {
      return {
        output: { plan: null, result: null, refused: true },
        frames: [{ type: "refuse", data: { reason: "out-of-schema" } }],
        status: "refused",
      };
    }
    const plan = { op: picked.class, executor: cfg.executor };
    // Execute deterministically over the exact store where the op is a closed op.
    const r = plugins.memory.executeOp(picked.class, { entity: input.entity, slot: input.slot }, env.space_id);
    const frames: Frame[] = [{ type: "parse", data: { plan } }, { type: "done" }];
    return { output: { plan, result: { rows: r.rows, count: r.count } }, frames, status: "ok" };
  },
};
