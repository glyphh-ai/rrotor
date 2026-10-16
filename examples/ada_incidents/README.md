# incidents: an Ada model on rrotor

An app's incidents, remembered. `spec.json` says what an incident is (the
service, endpoint, region and version; the error's class, message and status;
when it opened; how many users and which tier; and, behind a clearance, the
cause, the owner and the runbook that fixed it). Each incident recorded with
the fix that resolved it becomes a precedent; a new error, straight from an
alert or a log, is answered from the incidents closest to it.

| Function | Does |
|---|---|
| `record` `{data, outcome?}` | Keeps an incident, with the fix that resolved it when known |
| `query` `{data}` | The leading fix, its confidence, the precedents it stands on |
| `facts` `{data, top?}` | The closest past incidents, best first, cut to what the caller may see |
| `probabilities` `{data}` | P(fix \| incident) over the nearest precedents |
| `decide` `{data}` | The fix when confident, with the nearest precedent's cause, owner and runbook (`last_time`) for a caller cleared to see them; else asks how it was fixed (`needs_input`) and records it |
| `start` | How many incidents the model holds |

An incident, as the spec reads it:

```json
{
  "error": {
    "where": {"service": "api", "endpoint": "/v1/checkout", "region": "iad", "version": "2.14.0"},
    "what": {"class": "TimeoutError", "message": "upstream payment gateway timed out after 30s", "status": 504, "id": "inc-1041"},
    "when": {"opened": "2026-10-16T09:12:00Z"}
  },
  "impact": {"scale": {"users": 1200, "tier": "pro"}},
  "resolution": {"fix": {"cause": "gateway rate limit hit on retry storm", "owner": "payments", "runbook": "RB-12 back off retries, raise circuit breaker"}}
}
```

The outcome is the fix applied: `rollback`, `scale up`, `rotate credentials`,
`raise circuit breaker`, `hotfix`. The `resolution` layer weighs 0.7: a caller
below that clearance (a status bot, a customer-facing assistant) gets the fix
and the confidence, never the cause, the owner or the runbook.

Why it matters to someone building an app: every error they have ever fixed
becomes an answer for the next one, deterministic and offline, in their own
runtime, callable from their alerting (`POST /v1/runs` with a runtime key) or
from Claude (`model_call` on the runtime's MCP).

Its tests run with `pytest examples/ada_incidents -o addopts=""` from the repo
(the kernel the tests use is `tests/kernel/`).
