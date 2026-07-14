/**
 * transport/sse.ts — Server-Sent Events transport for a run's execution.
 *
 * SSE is the zero-dependency, session-durable backbone (WebSocket layers bidirectional
 * control on top of the same event model). Two entry points:
 *
 *   streamRunLive  — run a rotor and stream `open → step* → terminal → done` as the
 *                    executor produces each StepRecord (via the drain seam). A tiny
 *                    run summary is persisted so the run can be replayed later.
 *   replayRun      — a client reconnects with its last cursor (SSE `Last-Event-ID`
 *                    or `?from=`); the identical sequence is rebuilt from the
 *                    persisted tape and only newer events are re-emitted.
 *
 * The `seq` field is the resume cursor; `id:` in each SSE frame carries it, so a
 * browser `EventSource` (or the SDK) resumes automatically after a dropped socket.
 */

import type * as http from "node:http";

import { execute, deriveRunId } from "../exec/executor.js";
import { buildBasicPlugins } from "../plugins/index.js";
import { toolModeFromLabels } from "../tools/index.js";
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
 *  the tool sandbox root, and an optional memory-scoping session id. */
export interface StreamContext {
  store: Stator;
  drain: DrainPlugin;
  workspace: string;
  session?: string;
}

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

// ── SSE framing ────────────────────────────────────────────────────────────

/** Open the SSE response: the headers that keep proxies from buffering the stream. */
export function sseHead(res: http.ServerResponse): void {
  res.writeHead(200, {
    "content-type": "text/event-stream; charset=utf-8",
    "cache-control": "no-cache, no-transform",
    connection: "keep-alive",
    // Defeat nginx/proxy response buffering so events flush immediately.
    "x-accel-buffering": "no",
  });
  // Advertise the protocol version as a comment before any event.
  res.write(`: ${WIRE_VERSION}\n\n`);
  res.flushHeaders?.();
}

/** Write one framed event: `id:` is the resume cursor, `event:` is the kind. */
export function sseWrite(res: http.ServerResponse, ev: WireEvent): void {
  res.write(`id: ${ev.seq}\nevent: ${ev.kind}\ndata: ${JSON.stringify(ev)}\n\n`);
}

// ── a drain that tees StepRecords to the SSE stream ──────────────────────────

/** Emit each StepRecord to a callback (the SSE writer) while still delegating to
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
    return { ready: true, detail: "sse stream", tier: "basic" };
  }
}

// ── live streaming ───────────────────────────────────────────────────────────

/**
 * Run `doc` and stream its execution over SSE. Resolves when the stream is closed.
 * The response is ended here — the caller must not write to `res` afterward.
 */
export async function streamRunLive(
  res: http.ServerResponse,
  doc: RotorDocument,
  inputs: Record<string, unknown>,
  ctx: StreamContext,
): Promise<void> {
  sseHead(res);
  const rotor = `${doc.metadata.name}@${doc.metadata.version}`;
  // Name the run up front so a client that drops after the first frame can still
  // reconnect via GET /runs/:id/events. Derivation matches the executor exactly;
  // if inputs are invalid it will re-throw inside execute and we emit an error frame.
  let runId = "";
  try {
    runId = deriveRunId(doc, inputs);
  } catch {
    /* execute() below surfaces the input error as a terminal frame */
  }
  let seq = 0;
  sseWrite(res, {
    seq: seq++,
    kind: "open",
    wire: WIRE_VERSION,
    run_id: runId,
    trace_id: runId ? traceId(runId) : "",
    rotor,
    session: ctx.session,
  });

  const drain = new StreamDrain((rec) => sseWrite(res, stepEvent(rec, seq++)), ctx.drain);
  const plugins = buildBasicPlugins({
    store: ctx.store,
    drain,
    tools: { root: ctx.workspace, mode: toolModeFromLabels(doc.metadata.labels) },
  });

  try {
    const result = await execute(doc, inputs, plugins, ctx.session ? { session: ctx.session } : undefined);
    // The `open` frame was written before we had a run_id; the terminal + done
    // frames carry the identity, and replay reconstructs `open` from the summary.
    sseWrite(res, terminalEvent(result, seq++));
    sseWrite(res, doneEvent(result, seq));
    await persistForReplay(ctx.store, doc, inputs, result);
  } catch (err) {
    // A transport/executor crash still closes the stream cleanly with an error frame.
    log.error("stream run error", { rotor, detail: (err as Error).message });
    sseWrite(res, { seq: seq++, kind: "error", code: "E_INTERNAL", detail: (err as Error).message, remediation: "Retry; if it persists, capture the trace and file a support bundle." });
    sseWrite(res, { seq, kind: "done", status: "failed", terminal: "error", outputs: {} });
  } finally {
    res.end();
  }
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

// ── resume / replay ──────────────────────────────────────────────────────────

/**
 * Replay a run's stream from the persisted tape, re-emitting only events with
 * `seq > fromSeq`. This is the durable-reconnect path: identical sequence to the
 * live stream, so the client stitches the two together seamlessly.
 *
 * Returns false (without writing) when the run is unknown, so the caller can 404.
 */
export async function replayRun(
  res: http.ServerResponse,
  runId: string,
  fromSeq: number,
  store: Stator,
): Promise<boolean> {
  const history: StepRecord[] = await store.history.read(runId);
  const summary = (await store.kvGet(`summary:${runId}`)) as RunSummary | undefined;
  if (history.length === 0 && !summary) return false;

  const rotor = summary?.rotor ?? "unknown@0.0.0";
  const terminal = {
    status: summary?.status ?? (history.some((h) => h.error) ? "failed" : "ok"),
    terminal: summary?.terminal ?? "end",
    outputs: summary?.outputs ?? {},
    error: summary?.error,
    interrupt: summary?.interrupt,
    history,
  };
  const sequence = rebuildSequence(
    { kind: "open", wire: WIRE_VERSION, run_id: runId, trace_id: traceId(runId), rotor, session: summary?.session },
    history,
    terminal,
  );

  sseHead(res);
  for (const ev of sequence) {
    if (ev.seq > fromSeq) sseWrite(res, ev);
  }
  res.end();
  return true;
}
