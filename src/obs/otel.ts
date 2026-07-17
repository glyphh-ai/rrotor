/**
 * OpenTelemetry export — dependency-light. Rather than pull the `@opentelemetry/*`
 * SDK into the runtime hot path, we emit OTLP/JSON directly: each drain envelope
 * (one per StepRecord) maps to an OTLP **span**, using OTel semantic conventions so
 * any OTel-compatible collector, Jaeger/Tempo/Honeycomb/Datadog backend, or a
 * generic OTLP receiver ingests it as-is. A deployment that wants the real SDK
 * drops an exporter behind the drain seam; this is the zero-dependency default.
 *
 * Determinism: trace/span ids are derived (src/obs/trace.ts), so the span identity
 * is stable across replay. The only wall-clock is the export timestamp, injected by
 * the caller — it is telemetry, never recorded into the tape.
 */

import type { DrainEnvelope } from "../plugins/interfaces.js";
import { spanContext } from "./trace.js";

/** OTel span status codes (0 UNSET, 1 OK, 2 ERROR). */
const OTEL_STATUS = { UNSET: 0, OK: 1, ERROR: 2 } as const;

/** Map a run status to an OTel span status code. */
function otelStatus(status: string): number {
  if (status === "failed") return OTEL_STATUS.ERROR;
  if (status === "ok") return OTEL_STATUS.OK;
  return OTEL_STATUS.UNSET; // refused / escalated / interrupted — a control outcome, not an error
}

type KeyValue = { key: string; value: Record<string, unknown> };

function attr(key: string, value: unknown): KeyValue {
  if (typeof value === "boolean") return { key, value: { boolValue: value } };
  if (typeof value === "number") return Number.isInteger(value) ? { key, value: { intValue: value } } : { key, value: { doubleValue: value } };
  return { key, value: { stringValue: String(value) } };
}

/**
 * One OTLP span (ResourceSpans → ScopeSpans → span) for a drain envelope. Times
 * are provided by the caller as unix-nanos strings (telemetry only). Attributes
 * follow OTel conventions plus a `rotor.*` namespace for domain fields.
 */
export function toOtlpSpan(env: DrainEnvelope, times?: { startUnixNano?: string; endUnixNano?: string }): Record<string, unknown> {
  const ctx = spanContext(env.run_id, env.step_id, env.attempt);
  const attributes: KeyValue[] = [
    attr("rotor.run_id", env.run_id),
    attr("rotor.step_id", env.step_id),
    attr("rotor.attempt", env.attempt),
    attr("rotor.logical_tick", env.logical_tick),
    attr("rotor.status", env.status),
  ];
  if (env.space_id) attributes.push(attr("rotor.space_id", env.space_id));
  if (env.agent) attributes.push(attr("rotor.agent.ref", env.agent.ref));
  if (env.principal) attributes.push(attr("enduser.id", env.principal.id));
  if (env.usage?.input !== undefined) attributes.push(attr("rotor.usage.input_tokens", env.usage.input));
  if (env.usage?.output !== undefined) attributes.push(attr("rotor.usage.output_tokens", env.usage.output));
  if (env.usage?.cost !== undefined) attributes.push(attr("rotor.usage.cost", env.usage.cost));

  const span: Record<string, unknown> = {
    traceId: ctx.trace_id,
    spanId: ctx.span_id,
    name: env.step_id,
    kind: 1, // SPAN_KIND_INTERNAL
    startTimeUnixNano: times?.startUnixNano ?? "0",
    endTimeUnixNano: times?.endUnixNano ?? "0",
    attributes,
    status: { code: otelStatus(env.status) },
  };

  if (env.error) {
    // OTel convention: error type/message as exception attributes + ERROR status.
    attributes.push(attr("error.type", env.error.name));
    if (env.error.category) attributes.push(attr("rotor.error.category", env.error.category));
    if (env.error.retryable !== undefined) attributes.push(attr("rotor.error.retryable", env.error.retryable));
    (span.status as Record<string, unknown>).message = env.error.cause ?? env.error.name;
  }

  return {
    resourceSpans: [
      {
        resource: { attributes: [attr("service.name", "rrotor")] },
        scopeSpans: [{ scope: { name: "rrotor.runtime" }, spans: [span] }],
      },
    ],
  };
}
