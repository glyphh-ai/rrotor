/**
 * The golden replay harness — the determinism guardrail every build phase gates
 * on (BUILD_PLAN.md Phase 0).
 *
 * Two properties are checked:
 *
 *  1. **Determinism** — two independent fresh runs of the same document + inputs
 *     produce identical outputs and an identical, tick-for-tick event history.
 *     Nothing in the control plane may read wall-clock, RNG, or unordered
 *     iteration (SPEC.md §6), so a fresh run is reproducible byte-for-byte.
 *
 *  2. **Replay** — re-executing against a stator that already holds the run's
 *     history returns recorded outputs step-for-step and appends no new records
 *     (SPEC.md §5.4). Leaf steps short-circuit from their record; composites
 *     re-run their handler so their children replay individually.
 *
 * The harness is assertion-library-agnostic: it returns structured comparisons
 * and lets the test decide how to assert.
 */

import { execute, type ExecuteOptions, type RunResult } from "../../src/exec/executor.js";
import { buildBasicPlugins } from "../../src/plugins/index.js";
import { InProcessStore, type Stator } from "../../src/exec/store.js";
import type { RotorDocument, StepRecord } from "../../src/types.js";

/** How each harness call obtains a stator. Defaults to the in-process backend;
 *  pass a SQLite factory to prove backend parity. */
export type StoreFactory = () => Stator;

const defaultStore: StoreFactory = () => new InProcessStore();

/** The parts of a StepRecord that MUST be reproducible across runs. Excludes
 *  nothing today (the record carries no wall-clock field) but is the single
 *  place to add exclusions if a nondeterministic field is ever introduced. */
export interface NormalizedRecord {
  run_id: string;
  step_id: string;
  attempt: number;
  logical_tick: number;
  input_hash: string;
  idempotency_key: string;
  status: string;
  output: unknown;
  frames: unknown;
  usage: unknown;
  error: unknown;
}

export function normalizeHistory(history: StepRecord[]): NormalizedRecord[] {
  return history.map((r) => ({
    run_id: r.run_id,
    step_id: r.step_id,
    attempt: r.attempt,
    logical_tick: r.logical_tick,
    input_hash: r.input_hash,
    idempotency_key: r.idempotency_key,
    status: r.status,
    output: r.output ?? {},
    frames: r.frames ?? [],
    usage: r.usage,
    error: r.error,
  }));
}

/** The reproducible projection of a whole run — what determinism/replay compare. */
export interface RunShape {
  status: string;
  terminal: string;
  outputs: Record<string, unknown>;
  history: NormalizedRecord[];
}

export function shapeOf(result: RunResult): RunShape {
  return {
    status: result.status,
    terminal: result.terminal,
    outputs: result.outputs,
    history: normalizeHistory(result.history),
  };
}

/** Run a document once against a fresh, isolated basic-tier stator. */
export async function runFresh(
  doc: RotorDocument,
  inputs: Record<string, unknown>,
  opts: ExecuteOptions = {},
  storeFactory: StoreFactory = defaultStore,
): Promise<RunResult> {
  const store = storeFactory();
  try {
    return await execute(doc, inputs, buildBasicPlugins({ store }), opts);
  } finally {
    await store.close?.();
  }
}

export interface DeterminismResult {
  a: RunShape;
  b: RunShape;
  identical: boolean;
}

/** Run twice with two independent stators; report whether the shapes match. */
export async function checkDeterminism(
  doc: RotorDocument,
  inputs: Record<string, unknown>,
  opts: ExecuteOptions = {},
  storeFactory: StoreFactory = defaultStore,
): Promise<DeterminismResult> {
  const a = shapeOf(await runFresh(doc, inputs, opts, storeFactory));
  const b = shapeOf(await runFresh(doc, inputs, opts, storeFactory));
  return { a, b, identical: JSON.stringify(a) === JSON.stringify(b) };
}

export interface ReplayResult {
  first: RunShape;
  replay: RunShape;
  /** Records appended to the stator during the replay pass — MUST be zero. */
  appendedDuringReplay: number;
  identical: boolean;
}

/**
 * Execute once against a shared stator, then execute again against the *same*
 * stator (same doc + inputs → same run id → replay path). Reports whether the
 * replayed shape matches the first and how many records the replay appended.
 */
export async function checkReplay(
  doc: RotorDocument,
  inputs: Record<string, unknown>,
  opts: ExecuteOptions = {},
  storeFactory: StoreFactory = defaultStore,
): Promise<ReplayResult> {
  const store = storeFactory();
  try {
    // A fresh plugin bundle per pass (isolated per-run plugin state), but the
    // SAME shared store — exactly how the server serves runs across requests.
    const firstResult = await execute(doc, inputs, buildBasicPlugins({ store }), opts);
    const runId = firstResult.run_id;
    const before = (await store.history.read(runId)).length;

    const replayResult = await execute(doc, inputs, buildBasicPlugins({ store }), opts);
    const after = (await store.history.read(runId)).length;

    const first = shapeOf(firstResult);
    const replay = shapeOf(replayResult);
    return {
      first,
      replay,
      appendedDuringReplay: after - before,
      identical: JSON.stringify(first) === JSON.stringify(replay),
    };
  } finally {
    await store.close?.();
  }
}
