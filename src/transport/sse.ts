/**
 * transport/sse.ts — Server-Sent Events transport. The zero-dependency, session-durable
 * streaming face of a run: framing + the two HTTP entry points, over the shared
 * transport-agnostic core in run.ts.
 *
 *   streamRunLive — run a rotor and stream `open → step* → terminal → done` as SSE.
 *   replayRun     — a client reconnects with its last cursor (`Last-Event-ID` or
 *                   `?from=`); the identical sequence is replayed from the tape.
 *
 * `id:` in each SSE frame carries the `seq` cursor, so a browser `EventSource` (or
 * the SDK) resumes automatically after a dropped socket.
 */

import type * as http from "node:http";

import { WIRE_VERSION, type WireEvent } from "./events.js";
import { executeToEvents, buildReplay, type StreamContext } from "./run.js";
import type { Stator } from "../exec/store.js";
import type { RotorDocument } from "../types.js";

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
  try {
    await executeToEvents(doc, inputs, ctx, (ev) => sseWrite(res, ev));
  } finally {
    res.end();
  }
}

/**
 * Replay a run's stream from the persisted tape, re-emitting only events with
 * `seq > fromSeq`. Returns false (without writing) when the run is unknown, so the
 * caller can 404.
 */
export async function replayRun(
  res: http.ServerResponse,
  runId: string,
  fromSeq: number,
  store: Stator,
): Promise<boolean> {
  const events = await buildReplay(runId, fromSeq, store);
  if (!events) return false;
  sseHead(res);
  for (const ev of events) sseWrite(res, ev);
  res.end();
  return true;
}
