# Supportability runbook

How to diagnose and resolve a failing OpenRotor run — built so a human **or an AI
dev-ops agent** can go from symptom to fix without reading source. Every failure is
tagged with a stable **code** and every run with a stable **trace_id**; those two
handles drive everything below.

## The two handles

| Handle | What it is | Where it appears |
| --- | --- | --- |
| **`code`** | a taxonomy error code (e.g. `E_UNGROUNDED`) → [`docs/errors.md`](errors.md) | the failing step's `error.name`, log records, drain events, the `/run` response |
| **`trace_id`** | the run's W3C trace id (derived from `run_id`, replay-stable) | `RunResult.trace_id`, every drain envelope, the `/run` response, `openrotor support` |

Because the `trace_id` is *derived* from the run identity, a replay of the same run
reproduces the same trace — so you can line a replay up against the original span for
span.

## Triage in three steps

1. **Get the code + trace.** From a CLI run they print inline; from the HTTP API
   they are in the `/run` response `error` block and `trace_id`; from a collector,
   filter on `rotor.status = failed`.
2. **Look up the code.** `openrotor errors <CODE>` (or [`docs/errors.md`](errors.md))
   gives the category, whether it is retryable, and a concrete remediation.
3. **Pull the run.** `openrotor support <run_id>` prints the full timeline + every
   step's error + fix. `--json` gives the bundle to attach to a ticket or feed an
   agent.

## The commands

```
openrotor errors                 # the whole catalog (human table)
openrotor errors E_UNGROUNDED    # one code: summary + remediation
openrotor errors --json          # the machine-readable catalog (for agents/dashboards)
openrotor support <run_id>       # a run's trace + timeline + errors + fixes
openrotor support <run_id> --json
```

`support` reads the durable stator, so it requires
`ROTOR_STATOR_BACKEND=sqlite|pgvector` (an in-memory run is per-process and leaves
nothing to pull).

## Reading a failure

A failed CLI run ends with a block like:

```
  ✗ error:  E_UNGROUNDED  (grounding, retryable=false, severity=error)
            no fact for (ada, city)
            ↳ fix: This is the anti-fabrication backstop working. Write the supporting fact…
            ↳ see: docs/errors.md#codes  ·  `openrotor errors E_UNGROUNDED`
```

- **category** tells you *how to respond* (validation → fix input; transport →
  retry/check the endpoint; grounding → the data is missing; determinism → a
  correctness bug).
- **retryable** tells you whether the runtime already retried (transport /
  persistence / timeout) or whether retrying is pointless.
- **remediation** is the concrete next action.

## Observability wiring

- **Logs** (`src/obs/logger.ts`): structured, `ROTOR_LOG_FORMAT=json` for ingestion;
  every run summary carries `run_id`, `trace_id`, `status`, and the error code.
- **Drain** (`src/plugins/drain.ts`): one enriched envelope per step — carries the
  trace context and the error's category/severity/retryable/remediation.
- **OTLP** (`src/obs/otel.ts`): `toOtlpSpan()` exports OTel-native spans to any
  collector; failed steps map to ERROR spans with the code as `error.type`. See
  [`docs/observability.md`](observability.md) §3a.

## Escalation

If the code is `E_INTERNAL`, `E_HANDLER`, or a determinism code
(`E_REPLAY_DIVERGENCE`, `E_UNMERGEABLE`), it is a defect, not a config issue: capture
the `openrotor support <run_id> --json` bundle (it has the run_id, trace_id, and the
cause chain) and file it. Those codes' remediation text says the same.
