/**
 * E3 — W3C trace context + OTLP export. Trace/span ids are derived (not random),
 * so tracing is deterministic and replay-stable, and never perturbs the tape.
 */

import { describe, it, expect } from "vitest";

import { traceId, spanId, traceparent, spanContext } from "../../src/obs/trace.js";
import { toOtlpSpan } from "../../src/obs/otel.js";
import { toEnvelope } from "../../src/plugins/drain.js";
import type { StepRecord } from "../../src/types.js";

describe("W3C trace context (deterministic, derived)", () => {
  it("trace id is 32 hex, span id 16 hex, traceparent is well-formed", () => {
    const t = traceId("run-abc");
    const s = spanId("run-abc", "step1", 0);
    expect(t).toMatch(/^[0-9a-f]{32}$/);
    expect(s).toMatch(/^[0-9a-f]{16}$/);
    expect(traceparent(t, s)).toBe(`00-${t}-${s}-01`);
  });

  it("is stable for the same identity and distinct across runs/steps", () => {
    expect(traceId("run-abc")).toBe(traceId("run-abc")); // replay-stable
    expect(traceId("run-abc")).not.toBe(traceId("run-xyz"));
    expect(spanId("run-abc", "a", 0)).not.toBe(spanId("run-abc", "b", 0));
    expect(spanId("run-abc", "a", 0)).not.toBe(spanId("run-abc", "a", 1)); // per attempt
  });

  it("the whole run shares one trace id; each step is its own span", () => {
    const a = spanContext("run-1", "ask", 0);
    const b = spanContext("run-1", "plan", 0);
    expect(a.trace_id).toBe(b.trace_id);
    expect(a.span_id).not.toBe(b.span_id);
  });
});

function rec(over: Partial<StepRecord> = {}): StepRecord {
  return {
    run_id: "run-1",
    step_id: "ask",
    attempt: 0,
    logical_tick: 0,
    input_hash: "h",
    idempotency_key: "k",
    definitionVersion: "0.1.0",
    status: "ok",
    output: {},
    frames: [],
    ...over,
  } as StepRecord;
}

describe("drain envelope carries trace context", () => {
  it("stamps trace_id/span_id/traceparent derived from the record", () => {
    const env = toEnvelope(rec());
    expect(env.trace_id).toBe(traceId("run-1"));
    expect(env.span_id).toBe(spanId("run-1", "ask", 0));
    expect(env.traceparent).toBe(`00-${env.trace_id}-${env.span_id}-01`);
  });
});

describe("OTLP span export (OTel-native, no SDK)", () => {
  it("maps an ok step to an OTLP span with OK status and rotor.* attributes", () => {
    const otlp = toOtlpSpan(toEnvelope(rec({ space_id: "sp1" }))) as any;
    const span = otlp.resourceSpans[0].scopeSpans[0].spans[0];
    expect(span.traceId).toBe(traceId("run-1"));
    expect(span.status.code).toBe(1); // OK
    const keys = span.attributes.map((a: any) => a.key);
    expect(keys).toContain("rotor.run_id");
    expect(keys).toContain("rotor.step_id");
    expect(keys).toContain("rotor.space_id");
    expect(otlp.resourceSpans[0].resource.attributes[0].value.stringValue).toBe("openrotor");
  });

  it("maps a failed step to ERROR status with the taxonomy code as error.type", () => {
    const env = toEnvelope(rec({ status: "failed", error: { name: "E_TOOL", cause: "boom" } }));
    const otlp = toOtlpSpan(env) as any;
    const span = otlp.resourceSpans[0].scopeSpans[0].spans[0];
    expect(span.status.code).toBe(2); // ERROR
    expect(span.status.message).toBe("boom");
    const errType = span.attributes.find((a: any) => a.key === "error.type");
    expect(errType.value.stringValue).toBe("E_TOOL");
    expect(span.attributes.find((a: any) => a.key === "rotor.error.category").value.stringValue).toBe("transport");
  });

  it("maps a refused step to UNSET (a control outcome, not an error) and falls back to the code as message", () => {
    const env = toEnvelope(rec({ status: "refused", error: { name: "E_UNGROUNDED" } }));
    const span = (toOtlpSpan(env) as any).resourceSpans[0].scopeSpans[0].spans[0];
    expect(span.status.code).toBe(0); // UNSET
    expect(span.status.message).toBe("E_UNGROUNDED"); // no cause → code
  });

  it("encodes usage as int/double/bool/string attribute values and honors export times", () => {
    const env = toEnvelope(
      rec({
        principal: { id: "u1", kind: "user" },
        agent_identity: { ref: "glyphh/base@0.1.0", run_id: "run-1" },
        usage: { input: 10, output: 20, cost: 0.0025 },
      }),
    );
    const span = (toOtlpSpan(env, { startUnixNano: "5", endUnixNano: "9" }) as any).resourceSpans[0].scopeSpans[0].spans[0];
    expect(span.startTimeUnixNano).toBe("5");
    expect(span.endTimeUnixNano).toBe("9");
    const by = (k: string) => span.attributes.find((a: any) => a.key === k)?.value;
    expect(by("rotor.usage.input_tokens")).toEqual({ intValue: 10 }); // integer
    expect(by("rotor.usage.cost")).toEqual({ doubleValue: 0.0025 }); // double
    expect(by("enduser.id")).toEqual({ stringValue: "u1" });
    expect(by("rotor.agent.ref")).toEqual({ stringValue: "glyphh/base@0.1.0" });
  });
});
