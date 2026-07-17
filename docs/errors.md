# rrotor error catalog

The canonical taxonomy every runtime failure reports through (source of truth:
`src/errors.ts`; this file is generated — do not hand-edit). Each failure carries a
stable **code** you can pivot on: find the row, read the cause, apply the remediation.
Programmatic consumers (an AI dev-ops agent, a dashboard) can read the same data via
`errorCatalog()` or the drain envelope's enriched `error` block.

## How to use this

1. Grab the **code** and **trace_id** from the failing step's log record or drain event.
2. Look up the code below → **category** tells you how to respond, **retryable** whether
   the runtime already retried, **remediation** the concrete fix.
3. Correlate every step of the run with the **trace_id** (telemetry only — never in the
   deterministic tape).

## Categories

| Category | Meaning |
| --- | --- |
| `validation` | Bad caller input or spec — reject, don't retry. |
| `config` | Misconfiguration / missing wiring — fix the deployment. |
| `policy` | Governance / authorization denial — grant or stop. |
| `grounding` | Memory / HDC / space integrity — a data problem. |
| `transport` | External I/O (model, tool, drain, network) — often retryable. |
| `persistence` | The durable stator — often retryable. |
| `capacity` | Budgets, limits, timeouts — back off or raise the ceiling. |
| `determinism` | Replay / merge / translation invariant — a correctness bug. |
| `escalation` | An expected control signal (e.g. frontier declined). |
| `internal` | An unexpected defect — page someone. |

## Codes

| Code | Category | Retryable | Severity | HTTP | Summary | Remediation |
| --- | --- | :-: | --- | :-: | --- | --- |
| `E_MISSING_INPUT` | validation | no | error | 400 | A required `in` reference resolved to nothing. | Provide the missing input, or mark it optional in spec.inputs. Check the `$.` reference path in the failing step's `in`. |
| `E_TYPE` | validation | no | error | 400 | A value did not match its declared type. | Coerce the value to the declared type upstream, or widen the type in the spec. The context carries the expected vs actual type. |
| `E_SCHEMA` | validation | no | error | 400 | The rotor document failed schema or static-graph validation. | Run `rotor validate`; fix the reported path. Common causes: an unknown step type, a dangling `next`, or a gate without spec.space. |
| `OUT_OF_SCHEMA` | validation | no | error | 422 | A constrained decode produced a value outside the closed schema (§7.10). | This is the lattice refusing to invent an option. Widen the `ops`/enum if the value is legitimate, else treat as a genuine reject and refine the plan. |
| `E_UNKNOWN_STEP` | config | no | error | 500 | A `next`/entry referenced a step id that does not exist. | Fix the step graph: every `next` and `entry` must name a declared step or the reserved `end`. `rotor validate` catches this before run. |
| `E_NO_HANDLER` | config | no | error | 501 | No handler is registered for the step's type. | Register a handler for this step type, or remove the step. If it is a premium step type, ensure the premium handler bundle is installed. |
| `E_NO_TOOL` | config | no | error | 404 | A `tool` step named a method not present in the connections registry. | Register the tool/MCP method before the run, or correct the tool name. `connections.listTools()` shows what is available. |
| `E_POLICY_DENIED` | policy | no | error | 403 | Governance denied the step (step type, tool, model, or scope not granted). | Grant the capability in spec.access / the principal's grant set, or remove the step. The context names the denied capability. |
| `E_SCOPE_EXCEEDED` | capacity | no | error | 429 | A grant scope limit was exceeded. | Raise the scope limit for this principal/rotor, or reduce the work. Distinct from E_BUDGET_EXCEEDED (attention) — this is an authz scope. |
| `E_BUDGET_EXCEEDED` | capacity | no | error | 429 | The attention budget (revolutions / tokens) was exhausted (§10.4). | Raise spec.attention.budget, or set on_exhausted: escalate so the run climbs the ladder instead of failing. The context carries the budget dimension. |
| `E_TIMEOUT` | capacity | yes | error | 504 | An operation exceeded its time bound. | The runtime retries transient timeouts with backoff. Persisting: raise the timeout, check the downstream latency, or route to a faster lane. |
| `E_UNGROUNDED` | grounding | no | error | 422 | A ground gate found no supporting fact for the claim (§6.3). | This is the anti-fabrication backstop working. Write the supporting fact to the stator, or accept the refusal. The context carries (entity, role). |
| `E_SPACE_MISMATCH` | grounding | no | fatal | 409 | A cross-space HDC bind was attempted (§15.4). | All grounding in one run must share a space_id = sha256(vector_dim, encoder_seed, roles_config). Do not mix spaces; re-encode into the run's space. |
| `E_TRANSPORT` | transport | yes | error | 502 | An external dependency call failed at the transport layer. | The runtime retries with backoff. Persisting: check the endpoint URL, credentials, and the environment's network policy (see the proxy README). |
| `E_TOOL` | transport | yes | error | 502 | A registered tool/MCP method raised while executing. | Inspect the tool's error in the context/cause. Retry if transient; otherwise fix the tool inputs or the downstream service. Idempotent tools are safe to retry. |
| `E_STATOR` | persistence | yes | error | 503 | The durable stator (SQLite/Postgres) failed a read or write. | Check ROTOR_STATOR_URL and DB reachability/credentials. Writes are mirrored and retried; a persistent failure surfaces on flush. The run's in-memory mirror stays authoritative. |
| `E_UNTRANSLATABLE` | config | no | error | 501 | The gateway has no translation for this (from, to) pair (§8.3). | Add the missing entry to the gateway translation table, or route through a supported provider pair. Never a silent drop — this is the explicit refusal. |
| `E_UNMERGEABLE` | determinism | no | fatal | 500 | Two parallel branches produced conflicting writes to the same cell with no reducer (§7.17). | Declare a reducer for the contended state key, or partition the branches so they write disjoint cells. The context names the cell. |
| `E_REPLAY_DIVERGENCE` | determinism | no | fatal | 500 | Replay produced a different result than the recorded tape (§5.4). | A determinism violation — usually wall-clock/RNG leaking into the control plane, or an unpinned definition version. Pin the version; audit the diverging step for non-deterministic inputs. |
| `FrontierDeclined` | escalation | no | warning | 503 | The frontier lane declined the escalation (§9). | Expected control signal, not a defect: the run falls back to the configured lower rung (e.g. human). Ensure a fallback is set on the escalate step. |
| `E_HANDLER` | internal | no | error | 500 | A step handler threw a non-taxonomy error. | A defect in the handler — it should throw a RotorError. Inspect the cause chain; file with the run_id + step_id + trace_id from the log record. |
| `E_FAILED` | internal | no | error | 500 | A generic step failure with no more specific code. | Prefer a specific code. If you see this, the throw site needs a taxonomy code; capture the cause and report it. |
| `E_DEFAULT` | internal | no | error | 500 | A fallthrough default error. | Should not occur in normal operation; indicates an unhandled branch. Report with the run_id + trace_id. |
| `E_INTERNAL` | internal | no | fatal | 500 | An unexpected internal error (an unnormalized throw). | A bug. The cause chain has the original error; report with the run_id + step_id + trace_id and the stack from the log record. |

_24 codes._
