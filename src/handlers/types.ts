/**
 * The step-handler seam (docs/runtime.md §2.3). Dispatch is a closed catalog of
 * 20 types; the engine holds a `Map<StepType, StepHandler>` and each type maps
 * to exactly one subsystem verb. A handler receives `(step, input, env,
 * plugins, engine)` and returns a {@link StepResult} (output + frame stream);
 * the ENGINE records it, merges it, and picks the next step — the handler never
 * does (`select_next` owns control).
 *
 * Control-composing types (`loop`, `parallel`, `sub-rotor`) run other steps, so
 * they receive an {@link Engine} handle back into the run loop. Leaf handlers
 * ignore it.
 */

import type {
  RunContext,
  RunContextEnvelope,
  Step,
  StepResult,
  StepType,
} from "../types.js";
import type { Plugins } from "../plugins/interfaces.js";
import type { RunResult } from "../exec/executor.js";

/** The re-entrant handle a control-composing handler uses to run nested work. */
export interface Engine {
  /** The live per-run envelope (context, tick, space_id, identities). */
  readonly env: RunContextEnvelope;
  /** Execute a step by id — resolve → run → checkpoint → merge — and return its
   *  result. Used by `loop` bodies and `parallel` branches. */
  runStep(stepId: string): Promise<StepResult>;
  /** Recursively execute a referenced sub-rotor (§7.19). */
  runRotor(ref: string, inputs: Record<string, unknown>): Promise<RunResult>;
  /** Resolve a `$.` Context reference against the live Context. */
  resolve(ref: unknown, ctx?: RunContext): unknown;
}

export interface HandlerArgs {
  step: Step;
  /** The resolved, redacted `in` values. */
  input: Record<string, unknown>;
  env: RunContextEnvelope;
  plugins: Plugins;
  engine: Engine;
}

export interface StepHandler {
  readonly type: StepType;
  execute(args: HandlerArgs): Promise<StepResult>;
}
