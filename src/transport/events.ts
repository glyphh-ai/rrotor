/**
 * transport/events.ts — the **wire protocol** for streaming a run's execution.
 *
 * This is the contract the client SDK (and every thin client above it) consumes.
 * A run is streamed as an ordered sequence of {@link WireEvent}s, each carrying a
 * monotonic `seq` cursor. The sequence is deterministic and *reconstructible from
 * the persisted history tape* — that is what makes streaming **session-durable**:
 *
 *   - live:    the executor persists each StepRecord (executor.ts §append) BEFORE
 *              the drain fans it out, so every event a client sees is already on
 *              the tape.
 *   - resume:  on reconnect the client sends its last `seq` (SSE `Last-Event-ID`);
 *              the server rebuilds the identical sequence from the tape and re-emits
 *              only events with `seq` greater than the cursor. No lost or duplicated
 *              turns — the determinism thesis IS the reconnection story.
 *
 * The sequence shape is always: `open` → `step`* → (`answer` | `interrupt` |
 * `error`) → `done`. `seq` is the 0-based position in that sequence, so it is
 * identical whether produced live or replayed from the tape.
 */

import { describe as describeError } from "../errors.js";
import type { StepRecord } from "../types.js";

/** Bump on any breaking change to the event shape. The SDK negotiates on this. */
export const WIRE_VERSION = "rotor.stream/v1";

/** The terminal facts of a run needed to frame its close — satisfied by both a
 *  live {@link RunResult} and a replay summary rebuilt from the persisted tape. */
export interface RunTerminal {
  status: string;
  terminal: string;
  outputs: unknown;
  error?: { name: string; cause?: string };
  interrupt?: { stepId: string; awaiting?: unknown };
  history: StepRecord[];
}

/** One framed event in a run's stream. `seq` is the resume cursor. */
export type WireEvent =
  | { seq: number; kind: "open"; wire: string; run_id: string; trace_id: string; rotor: string; session?: string }
  | { seq: number; kind: "step"; step_id: string; type: string; status: string; frames: string[]; tick: number; error?: string }
  | { seq: number; kind: "answer"; text: string }
  | { seq: number; kind: "interrupt"; step_id: string; awaiting: unknown }
  | { seq: number; kind: "error"; code: string; detail: string; remediation: string }
  | { seq: number; kind: "done"; status: string; terminal: string; outputs: unknown };

/** Map a persisted StepRecord to a `step` event (the same shape the TUI renders). */
export function stepEvent(rec: StepRecord, seq: number): WireEvent {
  return {
    seq,
    kind: "step",
    step_id: rec.step_id,
    type: (rec as { type?: string }).type ?? rec.step_id,
    status: rec.status,
    frames: (rec.frames ?? []).map((f) => f.type),
    tick: rec.logical_tick,
    ...(rec.error ? { error: rec.error.name } : {}),
  };
}

/** The terminal frame for a run — derived from its outputs/error/interrupt. */
export function terminalEvent(result: RunTerminal, seq: number): WireEvent {
  if (result.status === "interrupted" && result.interrupt) {
    return { seq, kind: "interrupt", step_id: result.interrupt.stepId, awaiting: result.interrupt.awaiting };
  }
  if (result.error) {
    const d = describeError(result.error.name);
    return { seq, kind: "error", code: d.code, detail: result.error.cause ?? d.summary, remediation: d.remediation };
  }
  return { seq, kind: "answer", text: answerOf(result.history, result.outputs) };
}

/** The `done` control frame that closes every stream (success or failure). */
export function doneEvent(result: Pick<RunTerminal, "status" | "terminal" | "outputs">, seq: number): WireEvent {
  return { seq, kind: "done", status: result.status, terminal: result.terminal, outputs: result.outputs };
}

/**
 * Rebuild the full event sequence for a run from its persisted tape + a terminal
 * summary — the replay path. Identical order/seq to what the live stream emitted,
 * so a client resumes seamlessly by dropping events at or below its cursor.
 */
export function rebuildSequence(
  open: Omit<Extract<WireEvent, { kind: "open" }>, "seq">,
  history: StepRecord[],
  terminal: RunTerminal,
): WireEvent[] {
  const events: WireEvent[] = [{ ...open, seq: 0 }];
  let seq = 1;
  for (const rec of history) events.push(stepEvent(rec, seq++));
  events.push(terminalEvent(terminal, seq++));
  events.push(doneEvent(terminal, seq));
  return events;
}

/** Extract a human answer from a completed run: prefer `outputs.answer`, else the
 *  last step that wrote text. (Ported from the in-process TUI's `answerOf`.) */
export function answerOf(history: StepRecord[], outputs: unknown): string {
  const out = (outputs ?? {}) as Record<string, unknown>;
  if (typeof out.answer === "string" && out.answer) return out.answer;
  for (let i = history.length - 1; i >= 0; i--) {
    const o = history[i].output as { text?: unknown } | undefined;
    if (o && typeof o.text === "string" && o.text) return o.text;
  }
  return "";
}
