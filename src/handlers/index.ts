/**
 * The step-dispatch table (docs/runtime.md §2.3) — the closed catalog of 20
 * handlers, one subsystem verb per type. `buildHandlers()` returns the
 * `Map<StepType, StepHandler>` the executor holds; `select_next`, not any
 * handler, chooses the next step.
 */

import type { StepType } from "../types.js";
import { promptHandler, transformHandler } from "./compose.js";
import { modelHandler } from "./model.js";
import { gateHandler, assertHandler } from "./gate.js";
import {
  writeHandler,
  retrieveSqlHandler,
  retrieveKbHandler,
  retrieveVectorHandler,
  hdcMapHandler,
  cascadeHandler,
} from "./memory.js";
import { branchHandler, failHandler, waitHandler, planHandler } from "./control.js";
import { loopHandler, parallelHandler, subRotorHandler } from "./flow.js";
import { escalateHandler, toolHandler } from "./effects.js";
import type { StepHandler } from "./types.js";

export type { StepHandler, HandlerArgs, Engine } from "./types.js";
export { evalGate, type Verdict, type GateEval } from "./gate.js";

/** Every basic-tier handler, in catalog order. */
export const ALL_HANDLERS: StepHandler[] = [
  promptHandler,
  modelHandler,
  hdcMapHandler,
  writeHandler,
  retrieveSqlHandler,
  retrieveKbHandler,
  retrieveVectorHandler,
  gateHandler,
  assertHandler,
  planHandler,
  branchHandler,
  loopHandler,
  parallelHandler,
  waitHandler,
  escalateHandler,
  toolHandler,
  transformHandler,
  cascadeHandler,
  subRotorHandler,
  failHandler,
];

/** Build the closed dispatch table `Map<StepType, StepHandler>`. */
export function buildHandlers(overrides?: Partial<Record<StepType, StepHandler>>): Map<StepType, StepHandler> {
  const map = new Map<StepType, StepHandler>();
  for (const h of ALL_HANDLERS) map.set(h.type, h);
  if (overrides) for (const [t, h] of Object.entries(overrides)) if (h) map.set(t as StepType, h);
  return map;
}
