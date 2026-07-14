# Observability & Log Drains

How OpenRotor emits diagnostics and streams its run event history to external
sinks — and the invariants that keep both safe.

Spec: [SPEC.md §14 (Assurance & Observability)](../SPEC.md), §17.6 (telemetry
only, never a control predicate). Runtime: [docs/runtime.md §3.8](runtime.md).

---

## 1. Two channels

OpenRotor separates **diagnostics** from **the audit event stream**:

| Channel | What | Where |
| --- | --- | --- |
| **Structured logs** | operational diagnostics (boot, run summaries, warnings) | `src/obs/logger.ts` → stderr |
| **Log drain** | the run's `StepRecord` event stream (§5.4), forwarded to a sink | `src/plugins/drain.ts` → HTTP / file |

Both are **telemetry only**: nothing here ever feeds back into a run's control
flow. The deterministic run loop (SPEC.md §6) is unaffected by whether logging or
draining is enabled.

## 2. Structured logging

A zero-dependency structured logger. JSON when `ROTOR_LOG_FORMAT=json`, a compact
pretty line otherwise; minimum level from `ROTOR_LOG_LEVEL` (`debug|info|warn|error`,
default `info`). Run-scoped logs carry `run_id`/`step_id` via `logger.child(...)`.

```
{"level":"info","msg":"run complete","ts":"…","rotor":"glyphh/base@0.1.0","run_id":"run-…","status":"ok","steps":5}
```

The wall-clock timestamp lives only in log lines — **never** in a `StepRecord`,
which is keyed on the logical tick (§5.3) to stay replayable.

## 3. Log drains

A **drain** streams every `StepRecord` — the append-only, identity-stamped audit
trail — to an external destination for audit, FinOps, and observability. It is the
eighth capability seam (`drain`), reported in the manifest and `/readyz` like any
other.

### How it hooks in

Every record flows through one choke point — the executor's `append()`, right
after the durable write to the stator. There the executor calls
`plugins.drain.emit(record)`. Because `append()` runs **only on fresh execution**
(replay short-circuits before it), a replayed run **never re-emits** — no duplicate
telemetry.

`emit` is strictly fire-and-forget: it enqueues and returns, never blocks the run
loop, and never throws. A drain that is slow or down can never slow or fail a run.

### The envelope

Each record is serialized to a stable, CloudEvents-ish envelope
(`type: com.openrotor.step.v0`):

```json
{
  "type": "com.openrotor.step.v0",
  "run_id": "run-…", "step_id": "ask", "attempt": 0, "logical_tick": 0,
  "status": "ok", "space_id": "…",
  "principal": { "id": "local", "kind": "user", "scopes": ["…"] },
  "agent": { "ref": "glyphh/base@0.1.0", "run_id": "run-…" },
  "usage": { "input": 10, "output": 5 },
  "frames": [ { "type": "done" } ],
  "output": { "answer": "…" }
}
```

**Redaction:** top-level `output` field names listed in `ROTOR_DRAIN_REDACT` are
replaced with `"[redacted]"` before the envelope leaves the process. (Phase 5 will
route the governance layer's `spec.access` redaction through the same point.)

### Reliability

`BufferedDrain` provides the delivery guarantees, shared by every sink:

- **Batching** — envelopes are shipped in batches of `ROTOR_DRAIN_BATCH` (default
  50); a full batch auto-ships without blocking the run.
- **Backpressure** — a bounded buffer (`ROTOR_DRAIN_BUFFER`, default 10 000). When
  full, the **oldest** envelope is dropped and a counter increments (surfaced in
  `status()` and logged) — the run never blocks.
- **Retry** — a failed batch retries with exponential backoff; after exhausting
  retries the batch is dropped (counted), never retried forever, never thrown.
- **Flush on shutdown** — on `SIGTERM`/`SIGINT` the server flushes the buffer
  before exit, within the pod's `terminationGracePeriodSeconds` (30s) window.

### Sinks

| Sink | Selected by | Behavior |
| --- | --- | --- |
| **HTTP** | `ROTOR_DRAIN_URL` | `POST` batched newline-delimited JSON; `ROTOR_DRAIN_TOKEN` → `Authorization: Bearer` |
| **File** | `ROTOR_DRAIN_FILE` | append NDJSON (sidecar tailing) |
| **None** | neither set | `NoopDrain` — a ready seam that forwards nowhere |

## 3a. Trace context & OpenTelemetry

Every drain envelope carries **W3C Trace Context** (`trace_id`, `span_id`,
`traceparent`), and each `RunResult` carries its `trace_id`. The run is the trace;
each `(step_id, attempt)` is a span. Source: `src/obs/trace.ts`.

**These ids are derived from the run identity via sha256, not generated.** That is
deliberate and buys two things at once:

- **Determinism-safe.** No RNG, no wall-clock — so turning tracing on cannot perturb
  replay. The ids live only on logs/drain/OTLP, never in the tape or a control
  decision.
- **Replay-stable correlation.** A replayed run reproduces the *same* `trace_id` and
  per-step `span_id`s, so a support engineer can line a replay up against the
  original trace span for span.

**OpenTelemetry export.** `src/obs/otel.ts` → `toOtlpSpan(envelope)` maps a drain
envelope to an **OTLP/JSON span** using OTel semantic conventions (`service.name`,
`error.type`, `enduser.id`) plus a `rotor.*` namespace (`rotor.run_id`,
`rotor.step_id`, `rotor.status`, `rotor.usage.*`, `rotor.error.category`). Run status
maps to OTel span status (`ok`→OK, `failed`→ERROR with the taxonomy code as
`error.type`; `refused`/`escalated`/`interrupted`→UNSET, since those are control
outcomes, not errors). This is the **dependency-light** default — any OTLP collector
(Jaeger, Tempo, Honeycomb, Datadog, …) ingests it with no `@opentelemetry/*` SDK in
the runtime. A deployment that wants the full SDK drops an exporter behind the drain
seam.

To trace a run: grab the `trace_id` from the `RunResult` (or the `code` + `trace_id`
from a failing step's drain event / log line), then pivot in your OTLP backend.

## 4. Configuration

Non-secret settings live in `deploy/k8s/configmap.yaml`; the drain **token** lives
in `deploy/k8s/secret.yaml` (never the ConfigMap).

| Env var | Meaning | Default |
| --- | --- | --- |
| `ROTOR_LOG_FORMAT` | `json` \| `pretty` | `pretty` |
| `ROTOR_LOG_LEVEL` | `debug\|info\|warn\|error` | `info` |
| `ROTOR_DRAIN_URL` | HTTP sink endpoint | — (no drain) |
| `ROTOR_DRAIN_TOKEN` | bearer token for the HTTP sink (**Secret**) | — |
| `ROTOR_DRAIN_FILE` | NDJSON file sink path | — |
| `ROTOR_DRAIN_BATCH` | envelopes per batch | 50 |
| `ROTOR_DRAIN_BUFFER` | max buffered envelopes | 10 000 |
| `ROTOR_DRAIN_REDACT` | comma-separated `output` fields to redact | — |

## 5. Invariants (do not break)

1. **Telemetry only, never control** (§17.6) — a drain observes; it must never
   influence a run's outputs or transitions.
2. **Determinism-neutral** — enabling a drain must not change any run's shape; the
   golden replay harness proves this in CI.
3. **Fire-and-forget** — `emit` never blocks and never throws into the run.
4. **No re-emit on replay** — only fresh executions emit.
