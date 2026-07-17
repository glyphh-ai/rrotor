/**
 * transport/run.ts — the transport-agnostic core that turns a run into a stream of
 * {@link WireEvent}s. Both SSE (sse.ts) and WebSocket (ws.ts) drive these two
 * functions; the only difference between transports is how an event reaches the wire.
 *
 *   executeToEvents  — run a rotor live, calling `emit` for each event as it is
 *                      produced (open → step* → terminal → done), and persist a
 *                      replay summary so the run can be reconnected later.
 *   buildReplay      — rebuild a run's full event sequence from the persisted tape
 *                      and return the slice after a cursor (null if the run is
 *                      unknown). The durable-reconnect path, shared by both transports.
 */

import { execute, deriveRunId } from "../exec/executor.js";
import { buildBasicPlugins, childPluginsFactory } from "../plugins/index.js";
import type { BasicModelsOptions } from "../plugins/models.js";
import { toolModeFromLabels, type ToolPack } from "../tools/index.js";
import { bundledRotorResolver } from "../rotors.js";
import { traceId } from "../obs/trace.js";
import { log } from "../obs/logger.js";
import {
  WIRE_VERSION,
  stepEvent,
  terminalEvent,
  doneEvent,
  rebuildSequence,
  type WireEvent,
} from "./events.js";
import type { Stator } from "../exec/store.js";
import type { DrainPlugin } from "../plugins/interfaces.js";
import type { CapabilityStatus } from "../runtime/registry.js";
import type { RotorDocument, StepRecord } from "../types.js";

/** Shared dependencies a stream needs: the durable store, the base telemetry drain,
 *  the tool sandbox root, an optional memory-scoping session id, and the model config
 *  (the control surface — role→endpoint registry the control plane injects). */
export interface StreamContext {
  store: Stator;
  drain: DrainPlugin;
  workspace: string;
  session?: string;
  models?: BasicModelsOptions;
  /** Host tool packs installed alongside the stdlib, same contract + mode gating. */
  packs?: ToolPack[];
  /** Resolve `sub-rotor` refs (§7.19). Defaults to the bundled rotor registry. */
  rotors?: (ref: string) => RotorDocument | undefined;
  /** Pin the run id (conversational turns pass a fresh one per invocation). */
  runId?: string;
}

/** Called once per event as a run streams. */
export type Emit = (ev: WireEvent) => void;

/** The persisted terminal summary, so a completed run replays faithfully. */
interface RunSummary {
  status: string;
  terminal: string;
  outputs: unknown;
  error?: { name: string; cause?: string };
  interrupt?: { stepId: string; awaiting?: unknown };
  rotor: string;
  session?: string;
}

/** A drain that tees each StepRecord to `emit` (the wire) while still delegating to
 *  the base telemetry drain — so streaming never robs logs/OTLP of their events. */
class StreamDrain implements DrainPlugin {
  readonly name = "drain";
  constructor(
    private readonly onRec: (rec: StepRecord) => void,
    private readonly base?: DrainPlugin,
  ) {}
  emit(rec: StepRecord): void {
    try {
      this.onRec(rec);
    } finally {
      this.base?.emit(rec);
    }
  }
  async flush(): Promise<void> {
    await this.base?.flush();
  }
  async close(): Promise<void> {
    await this.base?.close();
  }
  status(): CapabilityStatus {
    return { ready: true, detail: "stream", tier: "basic" };
  }
}

type ExecuteOptions = NonNullable<Parameters<typeof execute>[3]>;

/**
 * Run a rotor and emit its execution as an ordered event sequence (open → step* →
 * terminal → done), then persist a replay summary. Never throws — a transport or
 * executor crash is delivered as a terminal `error` + `done` so the caller can
 * always close the stream cleanly. Shared by the fresh-run and resume paths.
 */
async function streamExecution(
  doc: RotorDocument,
  inputs: Record<string, unknown>,
  ctx: StreamContext,
  emit: Emit,
  runId: string,
  execOpts: ExecuteOptions | undefined,
): Promise<void> {
  const rotor = `${doc.metadata.name}@${doc.metadata.version}`;
  let seq = 0;
  emit({
    seq: seq++,
    kind: "open",
    wire: WIRE_VERSION,
    run_id: runId,
    trace_id: runId ? traceId(runId) : "",
    rotor,
    session: ctx.session,
  });

  const drain = new StreamDrain((rec) => emit(stepEvent(rec, seq++)), ctx.drain);
  const plugins = buildBasicPlugins({
    store: ctx.store,
    drain,
    tools: { root: ctx.workspace, mode: toolModeFromLabels(doc.metadata.labels), packs: ctx.packs },
    ...(ctx.models ? { models: ctx.models } : {}),
  });

  try {
    const result = await execute(doc, inputs, plugins, {
      rotorResolver: ctx.rotors ?? bundledRotorResolver,
      pluginsFor: childPluginsFactory({ store: ctx.store, root: ctx.workspace, packs: ctx.packs }),
      ...execOpts,
    });
    emit(terminalEvent(result, seq++));
    emit(doneEvent(result, seq));
    await persistForReplay(ctx.store, doc, inputs, result);
  } catch (err) {
    log.error("stream run error", { rotor, detail: (err as Error).message });
    emit({ seq: seq++, kind: "error", code: "E_INTERNAL", detail: (err as Error).message, remediation: "Retry; if it persists, capture the trace and file a support bundle." });
    emit({ seq, kind: "done", status: "failed", terminal: "error", outputs: {} });
  }
}

/**
 * Run `doc` fresh and stream its execution. The run is named up front (same
 * content-address the executor uses) so a client that drops after the first frame
 * can still reconnect; invalid inputs re-throw inside execute() as a terminal error.
 */
export async function executeToEvents(
  doc: RotorDocument,
  inputs: Record<string, unknown>,
  ctx: StreamContext,
  emit: Emit,
): Promise<void> {
  let runId = ctx.runId ?? "";
  if (!runId) {
    try {
      runId = deriveRunId(doc, inputs);
    } catch {
      /* execute() below surfaces the input error */
    }
  }
  await streamExecution(doc, inputs, ctx, emit, runId, {
    ...(ctx.session ? { session: ctx.session } : {}),
    ...(runId ? { runId } : {}),
  });
}

/**
 * Continue an interrupted run from its persisted checkpoint (human-in-the-loop
 * approval), streaming the continuation. Returns false if there is no such paused
 * run. The continuation is its own stream; the full run replays via buildReplay.
 */
export async function resumeToEvents(
  runId: string,
  payload: Record<string, unknown>,
  ctx: StreamContext,
  emit: Emit,
): Promise<boolean> {
  const saved = (await ctx.store.kvGet(`run:${runId}`)) as
    | { doc: RotorDocument; inputs: Record<string, unknown>; interrupt: { stepId: string } }
    | undefined;
  if (!saved) return false;
  await streamExecution(saved.doc, saved.inputs, ctx, emit, runId, {
    runId,
    ...(ctx.session ? { session: ctx.session } : {}),
    resume: { stepId: saved.interrupt.stepId, payload },
  });
  return true;
}

/** Persist the interrupt (so resume works) + a terminal summary (so replay is faithful). */
async function persistForReplay(
  store: Stator,
  doc: RotorDocument,
  inputs: Record<string, unknown>,
  result: import("../exec/executor.js").RunResult,
): Promise<void> {
  if (result.status === "interrupted" && result.interrupt) {
    await store.kvSet(`run:${result.run_id}`, { doc, inputs, interrupt: result.interrupt });
  }
  const summary: RunSummary = {
    status: result.status,
    terminal: result.terminal,
    outputs: result.outputs,
    error: result.error ? { name: result.error.name, cause: result.error.cause } : undefined,
    interrupt: result.interrupt,
    rotor: `${doc.metadata.name}@${doc.metadata.version}`,
    session: (inputs.session as string | undefined) ?? undefined,
  };
  await store.kvSet(`summary:${result.run_id}`, summary);
}

/**
 * Rebuild a run's event sequence from the persisted tape and return the events with
 * `seq > fromSeq` — the durable-reconnect slice. Returns null if the run is unknown,
 * so the caller can 404. Identical order/seq to the live stream, so a client stitches
 * the reconnect onto what it already saw with no gap or duplicate.
 */
export async function buildReplay(runId: string, fromSeq: number, store: Stator): Promise<WireEvent[] | null> {
  const history: StepRecord[] = await store.history.read(runId);
  const summary = (await store.kvGet(`summary:${runId}`)) as RunSummary | undefined;
  if (history.length === 0 && !summary) return null;

  const rotor = summary?.rotor ?? "unknown@0.0.0";
  const sequence = rebuildSequence(
    { kind: "open", wire: WIRE_VERSION, run_id: runId, trace_id: traceId(runId), rotor, session: summary?.session },
    history,
    {
      status: summary?.status ?? (history.some((h) => h.error) ? "failed" : "ok"),
      terminal: summary?.terminal ?? "end",
      outputs: summary?.outputs ?? {},
      error: summary?.error,
      interrupt: summary?.interrupt,
      history,
    },
  );
  return sequence.filter((ev) => ev.seq > fromSeq);
}
