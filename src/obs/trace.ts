/**
 * Deterministic W3C Trace Context (https://www.w3.org/TR/trace-context/) for the
 * runtime's telemetry. The trace/span ids are **derived from the run identity via
 * sha256**, never random — which gives two enterprise-grade properties at once:
 *
 *  - **Determinism-safe.** No RNG, no wall-clock. These ids are telemetry only —
 *    they ride logs and the drain, and NEVER enter the append-only tape or a
 *    control decision (SPEC.md §17.6). Deriving them (rather than generating) means
 *    adding tracing cannot perturb replay.
 *  - **Replay-stable correlation.** A replayed run reproduces the *same* trace_id
 *    and per-step span_ids as the original, so a support engineer can line the
 *    replay up against the original trace one span at a time.
 *
 * The run is the trace; each `(step_id, attempt)` is a span within it.
 */

import { sha256 } from "../exec/util.js";

/** 16-byte trace id (32 lowercase hex), per W3C. Derived from the run id. */
export function traceId(runId: string): string {
  return sha256("trace", runId).slice(0, 32);
}

/** 8-byte span id (16 lowercase hex), per W3C. Derived from the step within the run. */
export function spanId(runId: string, stepId: string, attempt: number): string {
  return sha256("span", runId, stepId, String(attempt)).slice(0, 16);
}

/** The `traceparent` header value: `00-<trace>-<span>-01` (sampled). */
export function traceparent(trace: string, span: string): string {
  return `00-${trace}-${span}-01`;
}

/** The trace context for a single step — the shape stamped onto its telemetry. */
export interface SpanContext {
  trace_id: string;
  span_id: string;
  traceparent: string;
}

export function spanContext(runId: string, stepId: string, attempt: number): SpanContext {
  const t = traceId(runId);
  const s = spanId(runId, stepId, attempt);
  return { trace_id: t, span_id: s, traceparent: traceparent(t, s) };
}
