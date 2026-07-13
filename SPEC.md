# RotorSpec

**The open standard for deterministic AI agent loops.**

Spec version: **0.1** · Status: **Draft** · Reference executor: **glyphh-rotor**

The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHALL**, **SHALL NOT**, **SHOULD**,
**SHOULD NOT**, **RECOMMENDED**, **MAY**, and **OPTIONAL** in this document are to be
interpreted as described in [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119).

---

## 1. Overview & Goals

An AI agent is a loop. Today that loop lives inside a model's context window, is
re-derived from scratch on every prompt, and is impossible to replay, audit, or
reason about. **RotorSpec is the loop, lifted out of the model and made a
declarative artifact.**

A **rotor** is a versioned document describing a set of deterministic steps an
agent executes for a class of task. Most of the time that is the **base rotor**
— `ask → plan → execute → test`. But a team can declare domain-specific rotors:
web development, slide generation, prestige marketing campaigns, or "pull corp
data into a slim on-device model with a prompt call first and last." Rotors are
**composable, declarative, and deterministic.**

RotorSpec draws a hard line, borrowed from durable-execution systems (Temporal,
AWS Step Functions, LangGraph), that every credible workflow standard draws:

> A **deterministic control plane** — which step runs next, given the recorded
> state — is separated from a possibly-**stochastic data plane** — what a model
> or tool returns. The control plane is replayable. The data plane is
> checkpointed. Replay returns the recorded result; it never re-invokes the
> model.

### Goals

- **G1 — Determinism you can trust.** Given a rotor definition and a run's
  recorded event history, the *sequence of control-flow decisions* is
  reproducible, bit-for-bit, forever. (See §6 for what this does and does not
  promise about model tokens.)
- **G2 — A small, closed step vocabulary.** A minimal primitive set (modeled on
  Amazon States Language and the OpenAI Agents SDK "few primitives" ethos), with
  **one canonical serialization per construct**. Anthropic's five agent patterns
  (prompt chaining, routing, parallelization, orchestrator-worker,
  evaluator-optimizer) are *compositions* of these primitives, not new step
  types.
- **G3 — Grounding as a first-class gate.** RotorSpec is designed to carry the
  glyphh HDC grounding law: a model proposal is admitted only when an
  entity-keyed associative memory *returns it*. Refusal ("I don't know.") is a
  first-class terminal, not an error.
- **G4 — Honest determinism.** RotorSpec **MUST NOT** claim bit-reproducible
  model outputs. It promises deterministic control flow, replayable transcripts,
  idempotent effects, and *bounded* nondeterminism via gates.
- **G5 — Portability.** A Full-conformant rotor runs identically across engines.
  Engine-specific escape hatches **MUST NOT** appear in the portable document.
- **G6 — Graceful degradation.** A rotor **SHOULD** run, partially, on a bare
  box: missing Postgres, Redis, local model, or MCP SDK degrade a subsystem to
  `ready: false` rather than crashing the run.

### Non-goals

- Not a programming language. Control flow is declarative edges, not arbitrary code.
- Not a model. RotorSpec orchestrates models; it does not define them.
- Not a prompt format. A `prompt` step composes text; the spec does not dictate
  prompt content.

---

## 2. Terminology

| Term | Definition |
| --- | --- |
| **Rotor** | A versioned document (§4) declaring a named, ordered set of steps for a class of task. |
| **Step** | A single node in the rotor's transition graph, with a typed I/O signature, a `type` from the closed catalog (§7), and error-handling fields. |
| **Run** | One execution of a rotor against concrete inputs, identified by a `run_id`, pinned to a `definitionVersion`. |
| **Context** | The typed shared state that flows through a run. Steps read named references from it and write named outputs back, with declared reducers for fan-in (§5.2). |
| **Frame** | A typed event emitted by a step as it executes (`propose`, `parse`, `dispose`, `backtrack`, `gate`, `assert`, `refuse`, `delta`, `tool`, `cache`, `done`). A step's output contract is a *stream* of frames. The `cache` frame records a caching disposition — result-cache `hit`/`miss` (§5.7) or prompt-cache `write`/`hit`/`miss` (§8.6). |
| **Gate** | A step that wraps a stochastic input or output in a deterministic **accept / reject / escalate** contract: schema validation, an HDC ground check, a guardrail, an evaluator threshold, or a human approval pause. |
| **Escalation** | Moving work up the `local → frontier → human` ladder when a trigger fires (refuse, low margin, frontier-decline, gate-reject, budget-exceeded). |
| **Gateway** | The transport, mechanics, and governance layer (§8) that normalizes every input in and output out of a rotor: I/O adapters, MCP ↔ API ↔ format translation, rate limiting / throttling, and metering / FinOps. |
| **Attention** | The dual of escalation (§10): the **budget** bounding how long a rotor rotates (wall-clock, revolutions, tokens, cost) and the **weights** stating which document signals the rotor focuses on. |
| **Stator** | The fixed control structure a rotor rotates against — the declared step graph and its `select_next` router (§5.1). Attention weights bias routing over the stator; the attention budget bounds revolutions around it. |
| **Principal** | The authenticated caller a run executes under — a user or service account, established by the platform's device-auth session / JWT, carrying the scopes and grants the run may draw on (§11). |
| **Agent identity** | The rotor instance itself — the unit of attribution, audit, and inter-rotor calls, derived from `(namespace/name@version, run_id)`. Recorded, with the principal, in every StepRecord (§11). |
| **Grant** | The actual policy the platform confers on a principal (identity scopes, control allow/deny, routing, data access). **External, org-owned, and never in the portable document** (§11); the document only *declares* what it requires, deny-by-default. |
| **Policy** | The org-owned control plane (§13) that decides what a rotor may do: allow/deny over step types/tools/models/connections, required approval gates, routing, budget caps, and data access. Enforced by the runtime + gateway; a rotor can never self-grant it. |
| **Anomaly** | A first-class INPUT (injection/jailbreak) or OUTPUT (policy/PII/drift/ungrounded-assertion) signal raised by a `gate` (§7.8, §14.2) that can trigger escalation (§9), not a log line. |
| **Checkpoint** | The `StepRecord` appended to the run's event history after a step completes: its input hash, idempotency key, output, frames, status, `space_id`, and both identities (§11). |
| **Replay** | Re-executing the control plane against a recorded event history. Recorded step outputs are *returned*, never recomputed. |
| **Result cache** | Cross-run memoization of a step's output, content-keyed and bounded by `ttl` + `scope` (§5.7). A hit is recorded in the `StepRecord` like any other result, so replay stays deterministic. Distinct from within-run idempotency (§5.6). |
| **Prompt cache** | Provider-side prefix cache: marking a stable prompt prefix (system prompt, tool defs, long context) so the provider reuses it across calls to cut cost/latency (§8.6). Determinism-neutral — it never changes model output, only `usage`. |
| **Space** | The HDC vector space a run binds against, identified by `space_id = sha256(vector_dim, encoder_seed, roles_config)`. Binding across mismatched spaces returns noise and **MUST** be refused. |
| **Macro rotor** | One pass **per prompt**: `WRITE → RECALL → REASON → (ESCALATE)`. The React-Flow loop. |
| **Micro rotor** | One pass **per decoded word**: `propose → dispose → backtrack → assert\|refuse`, optionally hardened by a logit-time prefix automaton. |
| **Runtime instance** | A self-contained, **stateless** instance of this spec that serves runs (§17.1): all run-critical state lives in the stator, the gateway is its only transport boundary. Instances are **fungible** — any one can serve any run — which is what makes a runtime a horizontally-scaled Kubernetes Deployment of interchangeable pods. |
| **Pool** | The managed set of fungible **runtime instances** a runtime keeps to serve a rotor's runs (§17.2). Sized by a declared `spec.pool` block (§17.3) and **bounded by the §13.2 budget** — idle warm instances are a FinOps cost. |
| **Hot / Warm / Cold** | The three readiness states of a pooled instance (§17.2). **Hot** = fully warmed, serves immediately (local model loaded, stator + gateway connected, caches primed). **Warm** = loaded but idle/suspended; fast resume. **Cold** = not instantiated; the first request pays the full cold-start. |
| **Affinity** | A routing preference (§17.4) steering a request to a warm instance already holding matching state (`keys`: `tenant` / `conversation` / `entity`), so warm state + caches (§5.7) are reused, not re-primed. `mode: prefer` (default — soft, falls back to any instance) or `require` (hard pin). Determinism-neutral (§17.6): operational metadata, never a control input. |

---

## 3. Conformance Levels

RotorSpec defines three cumulative levels. A document or engine claims the
highest level it satisfies.

### Core (L1 — Declarative)

- The document is valid against the RotorSpec JSON Schema.
- Steps carry a `type` from the closed catalog and a typed I/O signature.
- Semantics are documentation-only; **no execution guarantee is required.**
- Use: sharing, review, code generation, diagrams.

### Standard (L2 — Executable)

Core, plus a single engine **MUST** be able to run the document with full step
semantics:

- All step types in §7 execute per their contracts.
- Per-step `retry` / `catch` (§5.5) are honored.
- Gates enforce accept/reject/escalate; every `loop` has a mandatory
  `max_iterations` or budget cap; every path reaches a terminal.
- Escalation (§9) routes on typed triggers.
- The **gateway** (§8) normalizes I/O, throttles per provider/rotor/step/tenant,
  and meters cloud calls; declared rate limits and metering caps are enforced.
- **Attention** (§10) budgets bound the run; on exhaustion the rotor stops,
  escalates, or emits best-effort, and attention weights bias routing without
  adding control edges.
- The **trust layer** (§11–§14) is enforced, deny-by-default: the run binds an
  authenticated **principal** and **agent identity** (§11); the granted **control
  policy** (§13.1) allow/deny is applied and required **approval gates** honored;
  **data-access grants** (§13.4) are enforced with ungranted fields **redacted
  before they enter the rotor's context**; **routing governance** (§13.3) is
  enforced at the gateway; and **anomaly gates** (§14.2) scan inputs and outputs
  and may escalate. An ungranted scope, policy, route, or field is refused, never
  assumed.
- Inputs/outputs are validated against declared types.

### Full (L3 — Portable / Replay-Conformant)

Standard, plus the run is portable and faithfully replayable across engines:

- **Deterministic control plane:** control flow depends **only** on recorded
  state. No wall-clock, RNG, unordered iteration, or un-recorded live reads
  influence a transition (§6.2).
- **Checkpoint + replay:** every step appends a `StepRecord`; replay returns
  recorded outputs (§5.4).
- **Idempotent effects:** every effectful step carries an idempotency key so
  retries and replay never double-fire a side effect (§5.6).
- **Recorded caching:** a cross-run result-cache decision (hit/miss) and a
  provider prompt-cache write/hit/miss are captured in the `StepRecord` at first
  execution; replay returns the recorded result and **never** re-consults a cache
  (§5.7, §8.6). Caching is a cost/latency optimization; it **MUST NOT** change a
  control-flow decision.
- **Space invariants:** every retrieval / gate / encode step carries and
  validates a `space_id` (§15.4).
- **Run-pinning + patch gates:** a run is pinned to its `definitionVersion`;
  editing a rotor uses patch gates so in-flight runs finish on their original
  definition (§16.3).
- **Metered, bounded replay:** gateway `usage`/cost is recorded per step and the
  attention-budget exhaustion event is checkpointed at the tick it fired, so
  replay reproduces the same throttling and stop/escalate decisions (§8.5, §10.1).
- **Recorded identity & grants:** every `StepRecord` carries the `principal` and
  `agent_identity` (§5.4, §11); the granted scope set is checkpointed at run start
  and sub-rotor identity **attenuation** (§11.3) is recorded, so replay sees the
  same authority the original run saw — a later re-grant or revocation **MUST NOT**
  change a recorded run. Org-cap exhaustion (§13.2) is checkpointed at the tick it
  fires, exactly like the attention budget.
- **Golden-transcript conformance:** the engine passes the RotorSpec conformance
  suite (recorded inputs + activity results → expected control-flow decisions).

### Runtime optimizations are optional at every level

**Pooling and affinity (§17) are OPTIONAL at every conformance level** — they are
not a fourth level and never a requirement for Core (L1). Like prompt caching
(§8.6), they are **determinism-neutral** runtime optimizations (§17.6): an engine
that ignores `spec.pool` / `spec.affinity` — cold-starting every run and routing
each request to any instance — remains conformant at whatever level it otherwise
meets, because it produces the identical run, only slower. A Standard (L2) or Full
(L3) engine that *does* pre-warm or affinity-route **MUST** honor the declared
`pool` / `affinity` semantics (§17.2, §17.4), keep the warm pool within the §13.2
budget (§17.3), and **MUST NOT** let the serving instance or its hot/warm/cold
state influence a control-flow decision (§17.6).

---

## 4. The Rotor Document

A rotor is authored in YAML or JSON. The two are interchangeable; the JSON
Schema is normative. A document has four top-level fields.

```yaml
apiVersion: rotor.glyphh.ai/v0.1   # spec + API version (§16)
kind: Rotor
metadata:
  name: base                       # rotor identity within a namespace
  version: 0.1.0                    # rotor document semver (§16)
  namespace: glyphh                 # OPTIONAL
  description: The default ask/plan/execute/test loop.
  labels:                          # OPTIONAL free-form
    domain: general
spec:
  inputs:                          # typed run parameters
    - name: prompt
      type: string
      required: true
    - name: entity
      type: string
      required: false
  space:                           # HDC space identity (§15.4)
    vector_dim: 10000
    encoder_seed: 42
    roles_config: universal-7x33
    # space_id is derived and validated; MAY be pinned here
  gateway:                         # transport + governance for all I/O (§8)
    rateLimit:
      - { scope: provider, key: anthropic, rpm: 50, on_exceed: queue }
    metering: { meter: frontier-only, local: unmetered }
  attention:                       # budget + signal weights (§10)
    budget: { revolutions: 6, tokens: 150000, on_exhausted: best-effort }
    weights: { $.inputs.entity: 1.0 }
  identity:                        # required principal + scopes — DECLARE only (§11)
    principal: { required: true, kind: [user, service] }
    scopes: [memory:read:relational, model:frontier:invoke]
    on_missing_scope: refuse
  policy:                          # control-plane surface the rotor requires (§13.1)
    requires:
      step_types: [write, retrieve.sql, prompt, model, gate]
      models: [glyphh-local]
    # the allow/deny GRANT is external, org-owned — never in this document (§11)
  access:                          # data-access the rotor requires; deny-by-default (§13.4)
    stator:
      read: { spaces: [general], roles: [relational.object] }
      write: { spaces: [general] }
    on_ungranted: redact           # ungranted fields redacted before entering context
  assurance:                       # effectiveness metrics + anomaly gates (§14)
    effectiveness: { metrics: [gate_pass_rate, escalation_rate, cost_per_success] }
    anomaly: { input: { mode: firewall }, output: { mode: anomaly } }
  cache:                           # default cross-run result-cache policy (§5.7)
    ttl: 1h                        # applied to cacheable steps unless overridden
    scope: rotor                   # run | rotor | tenant | global
  pool:                            # warm-pool INTENT — runtime-optional (§17.3)
    minHot: 3                      # hot floor; platform provisions vs demand + §13.2 budget
    maxHot: 20                     # hard cost ceiling
    targetConcurrency: 5           # requests/instance before warming another
  affinity:                        # warm-instance routing preference (§17.4)
    keys: [tenant, conversation, entity]
    mode: prefer                   # prefer (default, falls back) | require (hard pin)
  state:                           # typed shared-state schema (§5.2)
    schema:
      answer: { type: string }
      grounding: { type: object }
    reducers:
      grounding: merge             # fan-in reducer for concurrent writes
  steps:                           # the transition graph (§5.1, §7)
    - id: write
      type: write
      # ...
      next: recall
  outputs:                         # projections from final Context
    - name: answer
      from: $.state.answer
```

### 4.1 Top-level fields

| Field | Req | Meaning |
| --- | --- | --- |
| `apiVersion` | **MUST** | `rotor.glyphh.ai/vMAJOR.MINOR`. Selects spec semantics. |
| `kind` | **MUST** | `Rotor` (this spec) or `SubRotor` (a callable fragment, §15.3 equivalent shape). |
| `metadata.name` | **MUST** | Rotor identity within its namespace. |
| `metadata.version` | **MUST** | Semver of the *document* (§16). Runs pin to it. |
| `metadata.namespace` / `labels` / `description` | MAY | Cataloguing. |
| `spec.inputs` | **MUST** | Typed run parameters. Missing required inputs fail before step 1. |
| `spec.space` | SHOULD | HDC space identity. **REQUIRED** if any retrieval/gate/encode step is present. |
| `spec.gateway` | SHOULD | Transport + governance for all I/O: adapters, MCP ↔ API ↔ format translation, rate-limit/throttle, metering/FinOps (§8). |
| `spec.attention` | MAY | Run-wide attention: budget (time/revolutions/tokens/cost) + signal weights (§10). |
| `spec.identity` | SHOULD | **Declares** the required principal kind + scopes the rotor needs (§11). Deny-by-default; the concrete principal and granted scopes are supplied externally, never here. |
| `spec.policy` | MAY | **Declares** the control-plane surface the rotor uses — step types / tools / models / connections + expected approval gates (§13.1). The allow/deny **grant** is external, org-owned, and **MUST NOT** appear here. |
| `spec.access` | SHOULD | **Declares** the connection + stator data-access the rotor requires, scoped to connector/operation/field and to memory spaces/entities/roles (§13.4). Deny-by-default; ungranted fields are redacted before entering context. The actual **grant** is external. |
| `spec.assurance` | MAY | Configures effectiveness telemetry and input/output **anomaly** gates (§14). Portable config — carried in the document. |
| `spec.cache` | MAY | Document-level default **result-cache** policy — cross-run memoization (`key`/`ttl`/`scope`), refined or disabled per step (§5.7). |
| `spec.pool` | MAY | **Declares** the warm-pool **intent** — hot floor/ceiling, target concurrency, warm schedule, cold-start budget (§17.3). Runtime-optional; the platform *provisions* the actual pool against demand and **within the §13.2 budget**. |
| `spec.affinity` | MAY | **Declares** instance-affinity keys + fallback `mode` for warm-instance routing (§17.4). Runtime-optional; determinism-neutral (§17.6). |
| `spec.state` | SHOULD | Typed shared-state schema + reducers (§5.2). |
| `spec.steps` | **MUST** | Non-empty ordered list of steps (§5.1). |
| `spec.outputs` | SHOULD | Named projections from the final Context. |

**Declare / grant / enforce.** `spec.identity`, `spec.policy`, and `spec.access`
are **declarations of requirement**, not policy. The governing principle of the
trust layer (§11–§14), a direct consequence of portability (G5), is:

> **A rotor DECLARES what it requires (portable, in the document); the platform,
> on behalf of a principal, GRANTS the actual policy (external, org-owned — NOT in
> the portable document); the runtime ENFORCES at execution — deny-by-default. A
> rotor can never self-grant.**

So these fields say *what authority, control surface, and data the rotor needs*;
the matching **grant** lives in the org's policy store, keyed to the principal;
and the runtime + gateway (§8) check declaration against grant before any effect
fires. A requirement without a grant is **denied**, never assumed. `spec.assurance`
is the exception in kind — it is portable *config*, not a requirement — because
observability wiring is safe to carry in the document.

`spec.pool` (§17.3) is the same shape in the **operational plane**: it declares a
warmth *intent* the platform **provisions** — bounded by the §13.2 budget, never
self-granted capacity — while `spec.affinity` (§17.4) and `spec.assurance` are
portable *config*. All three are runtime-optional and determinism-neutral (§17.6),
so they never gate a run's correctness.

### 4.2 The step object

Every step shares a common envelope; `config` is typed by `type`.

```yaml
- id: ground                       # MUST — unique within the rotor
  type: gate                       # MUST — from the closed catalog (§7)
  in:                              # typed input signature (references into Context)
    entity: $.inputs.entity
    role: relational.object
    filler: $.state.candidate
  out:                             # typed output signature (names written to Context)
    grounded: boolean
    margin: number
  config:                          # type-specific (§7)
    mode: hdc-ground
    admit: 0.10
    margin: 0.05
  next: assert                     # default successor (id | terminal)
  retry: []                        # ordered retriers (§5.5)
  catch: []                        # ordered catchers (§5.5)
  idempotency: auto                # auto | <expr> | none (§5.6)
  cache:                           # OPTIONAL — cross-run result memoization (§5.7)
    key: auto                      # auto (reuse idempotency key) | <expr>
    ttl: 1h
    scope: rotor                   # run | rotor | tenant | global
  attention:                       # OPTIONAL — per-step budget/weights (§10)
    weights: { $.state.candidate: 0.9 }
  access:                          # OPTIONAL — narrow this step's data-access below spec.access (§13.4)
    stator: { read: { roles: [relational.object] } }
  assurance:                       # OPTIONAL — per-step anomaly gate config (§14)
    anomaly: { output: { mode: hdc-ground, on_ungrounded: refuse } }
  gateway: {}                      # OPTIONAL — per-step transport override (§8)
  affinity:                        # OPTIONAL — refine warm-instance affinity for this step (§17.4)
    keys: [conversation]           # most meaningful on a `sub-rotor` step (§17.5)
```

- `id` — **MUST** be unique within the rotor.
- `type` — **MUST** be from §7. Unknown types are invalid at Core.
- `in` / `out` — the DSPy-style **typed signature**. `in` references pull from
  Context (`$.inputs.*`, `$.state.*`, `$.steps.<id>.<out>`). `out` names are the
  keys the step writes back.
- `next` — the default successor. Reserved terminals: `end` (succeed),
  `__fail__` (typed failure). `branch`/`loop`/`gate` may override `next` per §7.
- `retry` / `catch` — §5.5. `idempotency` — §5.6.
- `cache` — OPTIONAL cross-run **result cache** (§5.7): memoize this step's output
  under a content-addressed `key` for `ttl`, shared at `scope`. Overrides or
  disables (`cache: none`) `spec.cache`. This envelope `cache` is **distinct** from
  the `config.cache` breakpoints a `model`/`prompt` step may carry for **prompt
  caching** (§8.6): the envelope `cache` memoizes the step's *result* across runs;
  `config.cache` marks a reusable prompt *prefix* for the provider. See §5.7 for
  the idempotency-vs-cache distinction.
- `attention` — OPTIONAL per-step attention (§10): focus this step's signals
  (`weights`) and/or bound its rotation (`budget`); overrides `spec.attention`.
- `access` — OPTIONAL per-step data-access refinement (§13.4). A step **MAY only
  narrow** `spec.access` (attenuate, never widen) — it cannot request a scope the
  document did not declare or the principal was not granted (§11).
- `assurance` — OPTIONAL per-step anomaly/effectiveness config (§14): e.g. an
  output `hdc-ground` anomaly gate on a `model` step; overrides `spec.assurance`.
- `gateway` — OPTIONAL per-step transport override (§8); inherits `spec.gateway`
  otherwise.
- `affinity` — OPTIONAL per-step affinity refinement (§17.4): adjust which warm
  instance the router prefers when this step's work dominates warmth. It is most
  meaningful on a `sub-rotor` step (§7.19), where it refines the callee's pool
  routing (§17.5); it overrides `spec.affinity` for the step and is
  determinism-neutral (§17.6) — it can never widen the granted authority (§11.3).

Every path through the graph **MUST** reach a terminal (`end`, a `fail` step, or
an `assert`/refuse terminal). Well-formedness is checkable statically.

---

## 5. The Execution Model

### 5.1 Ordering

The steps form a directed transition graph. Execution starts at the first step
in `spec.steps` (or an explicit `spec.entry`, if present). After a step
completes, the engine selects the next step:

1. A `branch` step evaluates ordered pure predicates over recorded state and
   transitions to the first match (or `default`).
2. A `gate`/`loop` may route to a catcher, a refinement target, or escalation.
3. Otherwise control follows the step's `next`.

**Ordering depends only on recorded state.** Iteration order over collections is
the collection's recorded order; maps are iterated by sorted key. Wall-clock,
RNG, and un-recorded live reads **MUST NOT** influence which step runs next
(§6.2).

### 5.2 State & context passing

The **Context** is the typed shared state (`spec.state.schema`) plus namespaced
step outputs. A step reads via its `in` references and writes via its `out`
names. This is the single highest-leverage design decision in the spec — the
state contract is mandatory precisely because parallel/map aggregation is
undefined without one.

**Reducers.** When two steps write the same state key concurrently (via
`parallel`/`loop` fan-out), the engine merges them with the declared reducer for
that key (`spec.state.reducers`). Supported reducers: `last-write-wins`,
`append`, `merge` (deep object merge), `sum`, `max`, `min`, `union`, or a named
sub-rotor reducer. A concurrent write to a key with **no** declared reducer is an
error (`E_UNMERGEABLE`), not a silent race.

### 5.3 The logical clock

A run advances a monotonic **logical step counter**. Every `StepRecord` and any
`Wait` is stamped with the logical tick, never wall-clock time. Timeouts and
backoff intervals are recorded as durations and evaluated against the recorded
tick on replay, so a replayed run makes identical waiting decisions.

### 5.4 Checkpointing & replay

After each step the engine appends a **`StepRecord`** to the run's append-only
**event history**:

```
StepRecord {
  run_id, step_id, attempt,
  logical_tick,
  input_hash,            # sha256 of canonicalized `in`
  idempotency_key,       # §5.6
  space_id,              # §15.4 (if applicable)
  principal,             # §11 — the authenticated caller (audit trail)
  agent_identity,        # §11 — the rotor instance (attribution)
  status,                # ok | refused | failed | escalated | interrupted
  output,                # the `out` values
  frames,                # the emitted frame stream
  error                  # typed error name + cause (if failed)
}
```

**Replay** re-runs the control plane against this history. For each step:

- If a `StepRecord` for `(step_id, attempt)` exists, its `output` is **returned
  as-is**. The model/tool is **not** re-invoked.
- Only steps with no record execute for real.

This makes a run replayable from any step even though the model is not
bit-reproducible: the *decisions* are recomputed, the *stochastic results* are
recorded. This is the LangGraph time-travel / Temporal event-history model.

### 5.5 Retries & catch

Fault handling is declarative per step, copied from ASL's shape:

```yaml
retry:
  - errors: [E_TRANSPORT, E_TIMEOUT]   # typed error names, matched in order
    interval_ms: 500
    max_attempts: 3
    backoff_rate: 2.0
catch:
  - errors: [FrontierDeclined]         # typed governance decline
    next: fall_back_local              # route, don't crash
  - errors: ["*"]
    next: escalate_human
```

- `retry` is an ordered list of **retriers**: a typed `errors` match, an
  `interval_ms`, `max_attempts`, and `backoff_rate`. The first matching retrier
  applies. Backoff intervals use the logical clock (§5.3).
- `catch` is an ordered list of **catchers**: a typed `errors` match → `next`
  step. The first matching catcher routes control.
- Error names are typed strings. `FrontierDeclined` (governance) is distinct
  from `E_TRANSPORT` (transport) and **MUST** route to a fallback lane, not a
  crash (§9).

### 5.6 Idempotency & effects

Every effectful step (`model`, `tool`, `write`, `escalate`, side-effecting
`sub-rotor`) carries an **idempotency key**:

```
idempotency_key = sha256(definitionVersion, step_id, canonical(in), space_id)
```

- `idempotency: auto` (default) derives the key as above (content-addressed
  memoization, à la Temporal `WorkflowId`).
- On retry or replay, if a completed effect with the same key exists, the
  recorded result is returned and the side effect is **not** re-fired.
- `idempotency: <expr>` pins the key to a caller-controlled value (e.g. an
  external request id). `idempotency: none` marks a step non-memoizable — such a
  step **MUST NOT** appear in a Full-conformant rotor.

Side effects **MUST** live inside typed, idempotent, checkpointed steps — never
baked into the orchestration layer — or replay/exactly-once breaks.

### 5.7 Result caching (cross-run memoization)

Idempotency (§5.6) dedupes an effect **within one run** — a retry or replay of
*this* run returns the recorded result and never re-fires the effect. A **result
cache** is the *cross-run* generalization: a `cache` policy lets two different
runs reuse the same recorded output when their content-addressed inputs match,
bounded by a time-to-live and a sharing scope. Idempotency and caching **compose**
— they share one key derivation; they differ only in lifetime and reach.

A cacheable step carries a `cache` block in its envelope (§4.2):

```yaml
cache:
  key: auto            # auto (reuse the §5.6 idempotency key) | <expr>
  ttl: 24h             # a duration; entry expires on the logical clock (§5.3)
  scope: rotor         # run | rotor | tenant | global
```

- **`key: auto`** (default) **reuses the §5.6 idempotency key** verbatim:
  `sha256(definitionVersion, step_id, canonical(in), space_id)`. The cache is not
  a second addressing scheme — it is the same content address with a longer
  lifetime and a wider reach. `key: <expr>` pins a caller-controlled key (e.g. a
  document digest) for steps whose reuse identity is coarser than their literal
  `in`.
- **`ttl`** is a duration recorded against the logical clock (§5.3); an expired
  entry is a miss. Absent `ttl` means "cache for the scope's natural lifetime."
- **`scope`** bounds who may reuse the entry (below).

#### Idempotency vs. result cache

| | Idempotency (§5.6) | Result cache (§5.7) |
| --- | --- | --- |
| **Purpose** | Exactly-once within a run; safe retry/replay | Reuse a result across *different* runs |
| **Lifetime** | The run's event history | `ttl` (may outlive the run) |
| **Reach** | This `run_id` only | `scope`: run \| rotor \| tenant \| global |
| **Key** | `sha256(defVersion, step_id, canonical(in), space_id)` | Same key when `key: auto` |
| **On match** | Recorded effect returned; not re-fired | Cached output returned; step not executed |
| **Applies to** | Every effectful step (mandatory at Full) | Cacheable steps that opt in / default (below) |

`scope: run` is therefore ≈ idempotency (this run only). The higher scopes widen
reuse: **`rotor`** shares across runs of the same rotor *version*, **`tenant`**
across an org, **`global`** everywhere. Because the key already content-addresses
`definitionVersion`, `canonical(in)`, and `space_id`, a wider scope is sound only
when those inputs **fully** determine the output — in particular `space_id` **MUST**
be part of the key at `tenant`/`global` scope so a cross-tenant hit can never bind
against a foreign HDC space (§15.4). An engine **MUST** refuse a `global`/`tenant`
cache entry whose key omits `space_id`.

#### Determinism: a cache hit is checkpointed, then replayed like any result

A cache **hit** is recorded in the `StepRecord` (§5.4) as the step's `output` at
first execution, with a frame noting the hit. This is the load-bearing rule that
keeps caching determinism-safe:

> On the **first** execution the engine consults the cache; on **replay** of that
> run it returns the recorded `StepRecord` output and **never re-checks the cache**.

So a later `ttl` expiry, eviction, or a different value under the same key can
**never** change a recorded run's transitions — replay is a pure function of the
event history (§5.4, §6.1), exactly as for a non-cached step. A cache miss that
then executes the step is likewise recorded, so the run is reproducible whether or
not the cache was warm.

#### Default cacheability

Cacheability follows the determinism class of the step type (Appendix A):

- **Pure / deterministic-given-store steps** — `retrieve.sql`, `retrieve.kb`,
  `retrieve.vector`, `hdc.map`, `transform`, and `tool` with `idempotency: auto`
  — **MAY** be cached by default: their output is a function of recorded inputs +
  store state, so a warm hit and a fresh execution agree by construction.
- **`model` result caching is opt-in.** Caching a `model` step freezes one
  stochastic sample and serves it to later runs; that is occasionally desirable
  (a frozen "golden" answer) but is a deliberate choice, so it is **off unless a
  `model` step declares `cache`**. (Prompt caching on a `model` step — reusing the
  prompt *prefix*, §8.6 — is a separate, always-available lever that does **not**
  freeze the sample.)
- **`cache: none`** disables caching on any step, overriding a `spec.cache`
  default.
- Steps with genuine side effects beyond memoizable I/O (`write`, `escalate`,
  side-effecting `sub-rotor`, `wait`) rely on idempotency (§5.6), **not** cross-run
  caching, and **MUST NOT** be result-cached at scopes wider than `run`.

```yaml
# A deterministic retrieval, shared tenant-wide for an hour:
- id: recall
  type: retrieve.sql
  in: { person: $.inputs.entity }
  out: { rows: array }
  config: { op: lookup, slot: relational.object }
  cache: { key: auto, ttl: 1h, scope: tenant }
  next: compose
```

---

## 6. Determinism (stated honestly)

### 6.1 What RotorSpec promises

1. **Deterministic control flow.** Given a definition and a recorded event
   history, the sequence of transitions is reproducible, always.
2. **Replayable transcripts.** Every step's output is checkpointed; a run
   replays from any step by returning recorded results (§5.4).
3. **Idempotent effects.** Idempotency keys guarantee retries/replay never
   double-fire a side effect (§5.6).
4. **Bounded nondeterminism.** Every stochastic step is wrapped by a **gate**
   that converts its output into a deterministic accept/reject/escalate contract
   (§7.8 `gate`).

**Caching is determinism-neutral by construction.** Both caching layers are pure
cost/latency optimizations recorded under the same discipline as §5.4:

- **Prompt caching** (§8.6) never changes model output — it changes only which
  prompt tokens the provider re-reads, reflected in `usage`. It cannot affect a
  transition.
- **Result caching** (§5.7) records a hit as the step's `StepRecord` output at
  first execution; replay returns that record and never re-consults the cache. A
  later expiry or eviction cannot change a recorded run.

A cache **MUST NOT** be read in a way that lets a live cache state — warm vs cold,
present vs evicted — influence a control-flow decision at replay.

### 6.2 What RotorSpec does NOT promise

**Bitwise-reproducible model tokens.** This is not achievable in general.
Temperature 0 is greedy in theory, but floating-point reduction-order drift,
batch/kernel scheduling, and provider-side model changes make it best-effort
even with a seed. Anthropic exposes no stable seed as of early 2026. Only
open-weight models on controlled single-batch deterministic kernels approach
true token determinism. **A rotor engine MUST NOT claim identical tokens across
runs.** It records tokens; it does not reproduce them.

Control flow therefore **MUST NOT** depend on un-recorded values:

- ❌ wall-clock time in a predicate — use the logical clock (§5.3).
- ❌ RNG not seeded-and-recorded.
- ❌ unordered map/set iteration — iterate by sorted key.
- ❌ a live tool read not written to the event history.

These are the canonical determinism-breaking anti-patterns (Temporal's #1).

### 6.3 Seeds and the HDC gate

Two mechanisms give a rotor *real* determinism around a stochastic model:

- **HDC atoms are deterministic per `(name, seed)`.** The glyphh encoder derives
  every atomic hypervector via `sha256(name) → seeded RNG → ±1`. Two writes of
  the same fact land in the same place; a `dispose`/`ground` gate's
  accept/reject verdict is a deterministic function of `(entity, role, filler,
  store-state, space_id)` — independent of the model that *proposed* the filler
  (§7.8).
- **The hard gate is a logit-time invariant.** `GroundedConstraint` masks the
  vocabulary to only the grounded continuations for `(entity, role)` or the
  refusal token. The model *cannot emit* a non-grounded token: "the guarantee
  lives in the wrapper, not the weights." Enforcement mode is a gate config:
  `hard` (logit mask, needs logit access) or `soft` (verify-then-refuse
  post-hoc, for API models without logit access).

The model seed, where a provider offers one, is recorded as a *best-effort*
field in the `StepRecord` — never a correctness guarantee.

---

## 7. Step Types

The catalog is **closed**: an engine **MUST** reject unknown `type` values at
Core. Anthropic's five patterns are compositions of these — not new types. For
each step: purpose, key config, I/O, and determinism.

Legend for **Determinism**: *Deterministic* = pure control-plane, same
output for same recorded input. *Stochastic-checkpointed* = data-plane; output
recorded and replayed, never recomputed. *Deterministic-given-store* = a
function of recorded store state + space_id.

### 7.1 `prompt` — compose a bounded prompt

Pure templating of a bounded system/user block from Context. No model call.

- **config:** `template`, `max_tokens` (budget cap), `blocks` (ordered sections),
  optional `cache` — **prompt-cache breakpoints** (§8.6) marking which composed
  `blocks` form a stable, cacheable prefix (system prompt, tool defs, long context).
- **in:** references to interpolate. **out:** `text`.
- **determinism:** Deterministic. Breakpoints are advisory metadata carried on the
  composed prompt; they change downstream cost/latency, never the composed text.

```yaml
- id: compose
  type: prompt
  in: { question: $.inputs.prompt, facts: $.steps.recall.rows }
  out: { text: string }
  config:
    blocks:                         # ordered sections; stable ones cache well
      - { name: system, text: "Answer using ONLY these grounded facts:" }
      - { name: facts,  text: "{{facts}}" }
      - { name: ask,    text: "Question: {{question}}" }
    max_tokens: 1200
    cache:                          # prompt-cache breakpoints (§8.6)
      breakpoints: [system, facts]  # cacheable prefix; `ask` varies per turn
  next: reason
```

### 7.2 `model` — a model call (the quarantined stochastic Task)

Invoke a language model on a lane (`local` free llama-server, or `frontier`
metered gateway). Optionally runs the **micro rotor** (word-level
propose/dispose/backtrack) when `ground` is set.

- **config:** `lane` (`local|frontier`), `model`, `max_tokens`, `temperature`,
  `tools`, `seed` (best-effort), optional `ground` (inline gate: `entity`,
  `role`, `enforcement: hard|soft`, `refusal`), `micro` (word-level rotor on/off),
  `max_backtracks`, optional `cache` — **prompt-cache breakpoints** (§8.6) marking
  the cacheable prompt prefix (system prompt, `tools` defs, long stable context).
- **in:** `prompt`/`text` + optional `entity`/`role`. **out:** `text`, `frames`,
  `usage` (including cache write/hit/miss token accounting, §8.5).
- **determinism:** Stochastic-checkpointed. Tokens recorded; not reproduced. If
  `ground.enforcement: hard`, emitted tokens are constrained to grounded
  continuations (deterministic-given-store admission).
- **caching (two distinct levers):** the `config.cache` breakpoints above are
  **prompt caching** — always available, cost/latency-only, they never freeze the
  sample. An envelope-level `cache` (§5.7) enables **result caching** of the whole
  step; on a `model` step this is **opt-in** because it freezes one stochastic
  sample for later runs.

```yaml
- id: reason
  type: model
  in: { text: $.steps.compose.text, entity: $.inputs.entity }
  out: { text: string, frames: array }
  config:
    lane: local
    model: glyphh-local
    ground: { role: relational.object, enforcement: hard, refusal: "I don't know." }
    micro: true
    max_backtracks: 8
    cache: { breakpoints: [system, tools] }   # prompt-cache the stable prefix (§8.6)
  next: verify
```

### 7.3 `hdc.map` — map NL → sim (the grounding bridge)

Encode natural-language facts into an HDC **cortex** vector without persisting:
`enricher → {layer:{role:value}} → _sanitize_universal → universal_role_fillers →
Encoder.encode → cortex`. Used for ad-hoc compare/verify.

- **config:** `schema: universal-7x33`, `enricher` (`heuristic|local|auto`),
  inherits `space`.
- **in:** `text` **or** pre-structured `facts`. **out:** `cortex` (vector),
  `slots` (`[(layer.role, value)]`), `dropped` (off-schema/empty rejects).
- **determinism:** Deterministic-given-store (atoms fixed per `(name, seed)`);
  `space_id` validated.

```yaml
- id: encode
  type: hdc.map
  in: { text: $.inputs.prompt }
  out: { cortex: vector, slots: array }
  config: { schema: universal-7x33, enricher: auto }
  next: retrieve
```

### 7.4 `write` — persist a fact (memory-write / WRITE macro step)

Persist facts to the substrate. `tell_raw(facts)` is pre-structured with **no
model**; `remember/absorb(text)` runs the enricher NL→slots. An optional `key`
opens a **versioned chain** (`is_current` supersession).

- **config:** `mode` (`raw|absorb`), `key` (versioning), `speaker`, inherits `space`.
- **in:** `facts` **or** `text`. **out:** `written` (count), `key`, `space_id`.
- **determinism:** Deterministic-given-store; idempotent by key (§5.6).

```yaml
- id: write
  type: write
  in: { text: $.inputs.prompt }
  out: { written: number }
  config: { mode: absorb, key: $.inputs.entity }
  next: recall
```

### 7.5 `retrieve.sql` — deterministic closed-op query (RECALL)

The **only** structured-query primitive. A closed op over the indexed
`fact_slots` table with `{layer.role: value}` conditions intersected via fixed
SQL templates. **No model-generated SQL** — runaway queries are excluded by
construction.

- **config:** `op` ∈ `lookup | prev | count | count_not | top | who | compare |
  refuse`; op params (`person`, `slot`, `conditions`, `value`, `a`, `b`, `k`);
  inherits `space`.
- **in:** op params. **out:** `rows`, `count`, `matched`.
- **determinism:** Deterministic-given-store. **Aggregates come only from the
  store — never decoded from vectors.**

```yaml
- id: recall
  type: retrieve.sql
  in: { person: $.inputs.entity }
  out: { rows: array }
  config: { op: lookup, slot: relational.object }
  next: compose
```

### 7.6 `retrieve.kb` — entity-keyed associative / graph recall (RECALL)

Entity-keyed retrieval over the knowledge base: HDC **probe/verify**
(`unbind + cleanup` over a per-role codebook) and the **EntityGraph**
(`node(entity)` = union of slot fills; `neighbors(entity)` = entities sharing a
`(layer, role, value)` edge). Grounding is entity-keyed, **never**
whole-sentence cosine — a `retrieve.kb` step **REQUIRES** an `entity` and a
`role`.

- **config:** `mode` (`probe | verify | node | neighbors`), `entity`, `role`,
  `topn`, `depth`, `margin`; inherits `space`.
- **in:** `entity`, `role`. **out:** `filler`/`rows`, `membership`, `margin`, `top`.
- **determinism:** Deterministic-given-store; `space_id` validated.

```yaml
- id: probe
  type: retrieve.kb
  in: { entity: $.inputs.entity, role: relational.object }
  out: { filler: string, margin: number }
  config: { mode: verify, margin: 0.05 }
  next: gate
```

### 7.7 `retrieve.vector` — semantic vector recall (RECALL)

Embed a query (nomic, OpenAI-compatible endpoint) and brute-force unit-dot rank
over stored turns/events. Thresholds: `> 0.35` semantic, `> 0.05` lexical.

- **config:** `top_k`, `kind` (`query|document`), `threshold`, `embed_model`.
- **in:** `query`. **out:** `hits` (ranked), `scores`.
- **determinism:** Stochastic-checkpointed at the *embedding* boundary (the
  embedding is recorded); ranking over recorded vectors is deterministic.

```yaml
- id: semantic
  type: retrieve.vector
  in: { query: $.inputs.prompt }
  out: { hits: array }
  config: { top_k: 8, kind: query, threshold: 0.35 }
  next: compose
```

### 7.8 `gate` — accept / reject / escalate (test/gate)

Wrap a stochastic step's input or output in a **deterministic** contract. Covers
schema validation, the HDC **dispose/ground** check, safety guardrails,
evaluator score thresholds, input/output **anomaly** scans (§14.2), **and**
human-in-the-loop pause (the same mechanism: validation-fail and approval-pause
both interrupt-and-checkpoint).

- **config:** `mode`
  (`hdc-ground | schema | assertion | evaluator | approval | firewall | anomaly`);
  for `hdc-ground`: `entity`, `role`, `admit: 0.10`, `margin: 0.05`,
  `enforcement: hard|soft`; for `evaluator`: `threshold`, `scorer`; for `firewall`
  (INPUT anomaly, §14.2): `scanner`, scanning prompt + connection/stator content;
  for `anomaly` (OUTPUT anomaly, §14.2): `checks` (`policy | pii | drift | ood`);
  routing: `on_pass` (next), `on_fail` (retry | catcher | `escalate`),
  `on_escalate`.
- **in:** the value under test. **out:** `verdict` (`pass|fail|escalate`),
  `margin`, `membership`, `anomaly` (type + score, §14.2).
- **determinism:** Deterministic-given-store (the verdict is a pure function of
  recorded state + space_id) for `hdc-ground`/`schema`/`assertion`;
  Stochastic-checkpointed for a `firewall`/`anomaly` scanner (its verdict is
  recorded, then replayed). An `approval` gate is an interrupt (§7.14).

```yaml
- id: verify
  type: gate
  in: { entity: $.inputs.entity, role: relational.object, filler: $.steps.reason.text }
  out: { verdict: string, margin: number }
  config:
    mode: hdc-ground
    admit: 0.10
    margin: 0.05
    on_pass: assert
    on_fail: escalate
  next: assert
```

### 7.9 `assert` — grounded terminal / refuse

Terminal outcome of a grounded chain: **assert** the first grounded
`(entity, role) = filler`, or **refuse** when the whole belief order is vetoed
(the empty-cell invariant). Refusal is first-class, not an error.

- **config:** `refusal` string, `empty_cell` (`refuse | escalate`).
- **in:** `filler`, `verdict`. **out:** `asserted` | `refused`, `text`.
- **determinism:** Deterministic-given-store.

```yaml
- id: assert
  type: assert
  in: { filler: $.steps.probe.filler, verdict: $.steps.verify.verdict }
  out: { text: string }
  config: { refusal: "I don't know.", empty_cell: refuse }
  next: end
```

### 7.10 `plan` — typed constrained decode + deterministic execute

The "model does language, lattice keeps authority" pattern. A model emits a
**typed Plan** whose slots are constrained to schema enums (out-of-schema →
refuse); the engine then **executes it deterministically** over the exact store.
Numbers come from the store — never decoded from vectors.

- **config:** typed slots `op` / `field` / `tier` / `region` / `k` (each an
  enum), `executor` binding, `on_out_of_schema: refuse`.
- **in:** `question`. **out:** `plan` (typed), `result` (exact).
- **determinism:** Plan decode is Stochastic-checkpointed (constrained); execute
  is Deterministic-given-store.

```yaml
- id: analytics
  type: plan
  in: { question: $.inputs.prompt }
  out: { plan: object, result: object }
  config:
    ops: [total, average, count, churn]
    executor: business.org
    on_out_of_schema: refuse
  next: end
```

### 7.11 `branch` — deterministic router (Choice)

Evaluate ordered **pure predicates** over recorded state; transition to the
first match or `default`. No side effects, no model call in a predicate.
Implements Anthropic "routing."

- **config:** `cases: [{ when: <pure-expr>, next: <id> }]`, `default: <id>`.
- **in:** referenced state. **out:** none (control only).
- **determinism:** Deterministic.

```yaml
- id: route
  type: branch
  config:
    cases:
      - when: "$.steps.classify.intent == 'analytics'"
        next: analytics
      - when: "$.steps.classify.intent == 'recall'"
        next: recall
    default: reason
```

*A semantic router (`SchemaGuard.classify`, nearest-prototype with an
`OUT_OF_SCHEMA` negative class, margin ≥ 0.05) is a `model`/`gate` pair feeding a
`branch` — not a special node type.*

### 7.12 `loop` — bounded evaluator-optimizer

Bounded iteration: `generate → evaluate/score → refine`, continuing **while a
gate rejects**, capped by a mandatory `max_iterations` or `budget`. Termination
is guaranteed. Implements Anthropic "evaluator-optimizer" and the micro rotor's
backtrack loop at macro scale.

- **config:** `body` (sub-graph or sub-rotor ref), `gate` (the scoring gate),
  `max_iterations` (**REQUIRED**), `budget` (tokens/credits), `on_exhausted`
  (`escalate | refuse | best-so-far`).
- **in:** seed state. **out:** `result`, `iterations`.
- **determinism:** Control (iterate/stop) Deterministic-given-state; body may be
  Stochastic-checkpointed.

```yaml
- id: refine
  type: loop
  config:
    body: draft_and_score
    gate: { mode: evaluator, threshold: 0.8 }
    max_iterations: 4
    on_exhausted: best-so-far
  next: end
```

### 7.13 `parallel` — fan-out + reducer fan-in (Map / Parallel)

Run branches concurrently or map a step over a collection, then **merge results
via a declared reducer** before continuing. Undeclared fan-in merge is an error
(§5.2). Implements Anthropic "parallelization" and orchestrator-worker fan-out.

- **config:** `mode` (`parallel | map`), `branches` **or** (`over`, `as`,
  `body`), `reducer` (per-key or step-level), `max_concurrency`.
- **in:** collection/branch inputs. **out:** merged state.
- **determinism:** Fan-out order recorded; fan-in Deterministic via the reducer.

```yaml
- id: gather
  type: parallel
  config:
    mode: parallel
    branches: [recall, semantic, probe]
    reducer: { grounding: merge }
  next: compose
```

### 7.14 `wait` / `interrupt` — pause, checkpoint, resume

Pause the rotor, persist state via checkpoint, resume on an external event
(human approval, callback token, timer, incoming signal). Unifies HITL,
escalation, and async waits (LangGraph `interrupt()`, Step Functions task
tokens).

- **config:** `on` (`approval | signal | timer | callback`), `token`,
  `timeout_ms`, `on_timeout` (`escalate | fail | resume`).
- **in:** the value awaiting approval. **out:** `resumed_with`.
- **determinism:** The pause/resume decision is Deterministic-given-history; the
  resume payload is a recorded external input.

```yaml
- id: approve
  type: wait
  in: { draft: $.steps.reason.text }
  out: { resumed_with: object }
  config: { on: approval, timeout_ms: 86400000, on_timeout: escalate }
  next: publish
```

### 7.15 `escalate` — local → frontier → human (the ladder)

Route a turn up the ladder when a trigger fires. `decide(body, picked_route)`
sends to `local` (free) or `frontier` (metered, governed). A
`FrontierDeclined(402/403/429)` is a **governance** decline — distinct from
transport failure — and **falls back to local**, not a crash. The human rung
surfaces the refusal.

- **config:** `trigger` (`refuse | low-margin | frontier-decline | gate-reject |
  budget-exceeded`), `to` (`frontier | human`), `frontier_model`, `gateway_url`,
  `auth_token`, `fallback` (`local | human | refuse`).
- **in:** the turn + reason. **out:** `lane`, `text`, `usage`.
- **determinism:** Lane selection Deterministic-given-trigger; the resulting
  model call is Stochastic-checkpointed. See §9.

```yaml
- id: escalate
  type: escalate
  in: { text: $.steps.compose.text, reason: $.steps.verify.verdict }
  out: { lane: string, text: string }
  config: { trigger: low-margin, to: frontier, fallback: local }
  next: verify
```

### 7.16 `tool` — deterministic MCP tool / side-effecting app method

Two flavors. **(a) `tool.mcp`:** one of the ~30 in-runtime substrate tools
(`think/ask/query/recall/history/inspect/amend/forget/consolidate/…`), each with
a JSON `inputSchema` — the model picks *what*, the tool guarantees *how*.
**(b) `tool.app`:** a client/app method over the loopback attach channel
(`panels.open/switch/close`, `layouts.open`, `apps.callTool/install`) — the
side-effectful "skill hooks." Both dispatch through the same
`HandlerRegistry {method: handler}`. Attach is **loopback-only**; nothing dials
into a personal machine.

- **config:** `flavor` (`mcp | app`), `name`/`method`, `args`/`params`
  (per `inputSchema`), inherits `space`.
- **in:** args. **out:** tool result.
- **determinism:** `tool.mcp` Deterministic-given-store; `tool.app`
  Stochastic-checkpointed (external UI effect), idempotent by key (§5.6).

```yaml
- id: open_panel
  type: tool
  config: { flavor: app, method: panels.open, params: { app: notes } }
  next: end
```

### 7.17 `transform` — pure Pass

Side-effect-free state reshape (rename/reshape/inject constants). Keeps
data-munging out of `model` steps and off the model. ASL `Pass`.

- **config:** `set` (constant injections), `map` (rename/reshape expr).
- **in:** references. **out:** reshaped names.
- **determinism:** Deterministic.

### 7.18 `cascade` — memory maintenance / compact

The memory-maintenance rotor: `short` (rotor turns verbatim + vec) → `mid`
(events, local-model summaries; `EVENT_SPAN=8`, `EVENT_TRIGGER=12`) → `long`
(lattice facts via `absorb` + 7×33 enrichment). Non-blocking; **waits** when the
local model is down. Runs on cadence, out of the request path.

- **config:** `span`, `trigger`, `recall_k`, `cadence`.
- **in:** none (reads history). **out:** `consolidated` (counts).
- **determinism:** Stochastic-checkpointed (summaries recorded); scheduling
  Deterministic on the logical clock.

### 7.19 `sub-rotor` — callable rotor / handoff (composition)

Delegate to a named rotor with typed inputs/outputs, or hand off control to
another agent. Enables specialist routing and reuse (GitHub reusable workflows,
Argo templates, Agents SDK handoffs).

- **config:** `ref` (`namespace/name@version`), `mode` (`call | handoff`),
  `inputs` (mapping), `space` (`inherit | <own>`).
- **in:** mapped inputs. **out:** the sub-rotor's `outputs`.
- **determinism:** Inherits the callee's level; `call` returns control, `handoff`
  transfers it.
- **identity:** both `call` and `handoff` **attenuate** identity — the callee runs
  under the caller's principal with the **intersection** of the caller's grants and
  its own declared requirement, never more (§11.3).

```yaml
- id: slides
  type: sub-rotor
  config:
    ref: glyphh/pptx-rotor@1.2.0
    mode: call
    inputs: { outline: $.state.outline }
    space: inherit
  next: end
```

### 7.20 `fail` — typed terminal failure

Explicit terminal carrying a typed error name/cause so callers and catchers can
match it. Reached via `next: __fail__` or as its own step.

- **config:** `error` (typed name), `cause`.
- **determinism:** Deterministic.

---

## 8. Gateway

The **gateway** is the transport, mechanics, and governance layer that sits
between every rotor step and the outside world. Where the execution model (§5)
governs *which step runs next*, the gateway governs *how every input enters and
every output leaves* a step. It is the mechanics of the transport routing layers,
not a single feature: intercepting a client UI (say, a Claude desktop app) is
**one** capability the gateway can expose, never its definition.

Any input in and any output out of a rotor crosses the gateway. Every effectful
boundary passes through it: `model` calls (§7.2), `escalate` calls (§7.15),
`tool.mcp` / `tool.app` dispatch (§7.16), and `retrieve.vector` embeddings
(§7.7). Deterministic-given-store steps (`retrieve.sql`, `retrieve.kb`, `hdc.map`,
an HDC `gate`) never leave the box and never touch the gateway.

The gateway has five responsibilities:

| Responsibility | What it does |
| --- | --- |
| **I/O adapters** (§8.2) | Normalize *every* input in and *every* output out to internal step I/O. |
| **Protocol/format translation** (§8.3) | Translate across MCP ↔ provider HTTP wire ↔ internal step I/O. |
| **Rate limiting & throttling** (§8.4) | Bound call rate per provider / rotor / step / tenant; queue + backoff. |
| **Metering & FinOps** (§8.5) | Cost accounting, budgets/caps, spend attribution; cloud metered, local free. |
| **Prompt caching** (§8.6) | Translate prompt-cache breakpoints to provider wire formats; account cache write/hit/miss usage. |

### 8.1 Position in the model

The gateway is a **layer**, not a step type — it does not appear in the §7
catalog. It is declared once as `spec.gateway` (§8.7) and governs the transport
of every effectful step's I/O. A step **MAY** carry its own `gateway` block to
override the document policy for that step (e.g. a tighter rate limit on one
`model` call); otherwise it inherits `spec.gateway`.

Because the gateway's transforms are deterministic and recorded as frames (§2),
replay (§5.4) returns the **normalized** I/O, never a fresh wire read — the
gateway sits **inside** the checkpoint boundary, not outside it.

### 8.2 Input / output adapters

Every input into a step and every output out of it is normalized by an
**adapter**: a declared, pure mapping between an external wire form and internal
step I/O. Adapters are what let a step's `in` / `out` signature (§4.2) stay
**wire-agnostic** — the same `model` step runs against a local `llama-server` or
a frontier Anthropic endpoint because the gateway adapts the wire form on each
side.

- An **input adapter** maps an incoming wire form (an MCP tool result, an
  Anthropic `messages` response, an OpenAI `chat-completions` chunk, a raw
  client-UI event) into the internal step I/O the rotor reads.
- An **output adapter** maps a step's internal output back out to the wire form a
  provider, tool, or client expects.

Adapters are deterministic transforms; their result is part of the recorded frame
stream, so replay returns the normalized value.

### 8.3 Format / protocol translation — MCP ↔ API ↔ format

The gateway is the single place that translates across **three** representations
of a turn:

- **MCP** — tool calls and results as JSON `inputSchema` messages (the `tool.mcp`
  surface, §7.16).
- **Provider HTTP wire** — Anthropic `/v1/messages` and OpenAI
  `/v1/chat-completions` request/response shapes.
- **Internal step I/O** — a rotor step's typed `in` / `out` (§4.2) and its frame
  stream (§2).

A single turn can cross all three. The **MCP → API → format → MCP → API** chain
is explicit:

```
MCP tool result ─▶[in-adapter]─▶ internal step I/O ─▶[out-adapter]─▶ provider /v1/messages
       ▲                                                                        │
       └──────────── frames ◀─[in-adapter]◀─ provider stream (deltas) ◀─────────┘
```

An MCP tool result is adapted to internal I/O, composed into a provider `messages`
request, the provider streams deltas back, those are normalized into frames, and
the result is re-emitted as an MCP result — the gateway performing the round trip
so the author writes **one** typed signature and **never** a wire format.
Translation is table-driven and deterministic; a missing translation is a typed
error (`E_UNTRANSLATABLE`), not a silent drop.

**Prompt-cache breakpoints translate here too.** The abstract breakpoints an
author declares on a `prompt`/`model` step (§7.1, §7.2, §8.6) are part of the same
normalization: the gateway lowers them to each provider's native prefix-cache wire
form —

- **Anthropic** — emit `cache_control: { type: "ephemeral" }` on the last content
  block of each cacheable prefix segment (system prompt, tool defs, stable
  context).
- **OpenAI** — automatic prefix caching: no wire annotation is emitted; the
  gateway instead **orders** the composed blocks so the cacheable prefix is stable
  and leading, and records the reported cached-prefix length.
- **Local (`llama-server`)** — map to the engine's KV-prefix reuse where available;
  otherwise a no-op (breakpoints are advisory, never required for correctness).

A breakpoint the target provider cannot honor degrades to a no-op (the prompt is
sent uncached), never `E_UNTRANSLATABLE` — caching is best-effort by definition.

### 8.4 Rate limiting & throttling

Limits are **hierarchical** — enforced per **provider**, per **rotor**, per
**step**, and per **tenant**. A request that would exceed a limit is **queued
with backoff** (not dropped); the backoff interval uses the logical clock (§5.3),
so throttling and queueing decisions are replay-stable. A sustained breach
surfaces as a typed trigger — `budget-exceeded` or `frontier-decline` (§9.1) —
routed up the ladder, never a crash. Rate limits and concurrency caps are
declared in `spec.gateway.rateLimit` (§8.7).

### 8.5 Metering & FinOps

The gateway is the **metering point** of a rotor:

- **Cost accounting.** Every metered call records `usage` (tokens/credits) and
  cost into its `StepRecord` (§5.4).
- **Cache-aware token accounting.** Prompt caching (§8.6) makes a single call's
  tokens **not** fungible, so `usage` **MUST** break them out by cache disposition,
  because each is priced differently by the provider:
  - **cache write** — tokens written into the provider prefix cache on a miss
    (typically billed at a *premium* over base input tokens);
  - **cache hit** — tokens served from the cache (billed at a steep *discount*);
  - **cache miss / uncached** — ordinary input tokens plus all output tokens.

  The `StepRecord` `usage` therefore carries at least
  `{ input, output, cache_write, cache_read }` token counts; spend is the sum of
  each priced at its own rate. A result-cache hit (§5.7), by contrast, records
  **zero** provider `usage` — the call never left the box.
- **Budgets / caps.** Per-run, per-rotor, and per-tenant spend caps; exhaustion
  fires the `budget-exceeded` trigger (§9.1) and interacts with the attention
  budget (§10.1). Cache-write and cache-read tokens count toward the budget at
  their respective rates.
- **Spend attribution.** Cost is attributed to rotor / step / tenant / space for
  FinOps reporting, with cache write/hit/miss broken out so a FinOps report can
  show the realized savings of prompt caching.

The load-bearing policy is the **cloud/local split**:

> Cloud LLM calls route **through the glyphh server** and are **metered**; local
> model calls go **straight to local models** and are **free by construction**.
> Local inference **MUST NOT** proxy the metered gateway.

This is the same metering point escalation's frontier rung names (§9.2): credits
burn only on the frontier lane, through the gateway. `metering: frontier-only`
(§8.7) is the default policy.

### 8.6 Prompt caching (provider prefix cache)

An agent loop re-sends a large, near-identical prompt prefix on every turn — the
system prompt, the tool definitions, and any long stable context rarely change
between revolutions. **Prompt caching** tells the provider to reuse that prefix
instead of re-reading it, and it is the **single biggest cost/latency lever in an
agent loop**: the variable suffix (the new turn) is a small fraction of the tokens.
It is a gateway concern because it lives entirely in the format-translation (§8.3)
and metering (§8.5) boundary — the rotor author declares *intent*, the gateway
realizes it per provider.

**Breakpoints.** An author marks the cacheable prefix with **cache breakpoints**
in a `prompt` or `model` step's `config.cache` (§7.1, §7.2). A breakpoint names the
last segment of a prompt prefix that is stable enough to cache — everything up to
and including it is a cache unit:

```yaml
- id: reason
  type: model
  in: { text: $.steps.compose.text, entity: $.inputs.entity }
  out: { text: string, frames: array, usage: object }
  config:
    lane: frontier
    model: claude-sonnet
    tools: [think, query, recall]
    cache:
      breakpoints: [system, tools]   # cache the system prompt + tool defs prefix
  next: verify
```

Here the system prompt and the tool definitions — identical across every turn of
the loop — are cached once and reused; only the per-turn user text is re-read.

**Wire translation (§8.3).** The gateway lowers each breakpoint to the provider's
native form: **Anthropic** `cache_control: { type: ephemeral }` on the prefix's
last block; **OpenAI** automatic prefix caching (stable leading blocks, no
annotation); local KV-prefix reuse where available. An unhonorable breakpoint
degrades to an uncached send, never an error.

**Metering (§8.5).** Cache **write** (miss that populates the cache), **hit**
(served from cache), and **miss** (uncached) tokens are priced differently and are
accounted **distinctly** in the step's `usage` and in spend attribution. A cold
first turn pays a write premium; every subsequent turn pays the discounted hit
rate — which is exactly the saving a FinOps report surfaces.

**Determinism (neutral).** Prompt caching **does not change model output** — it
changes only which prompt tokens the provider re-reads, and therefore cost and
latency. Its only recorded trace is in `usage` (§8.5). It introduces **no** new
control-flow input and is **determinism-neutral**: a run replays identically
whether the cache was warm, cold, or disabled. Contrast §5.7 result caching, which
*replaces* a step's execution with a stored output; prompt caching never does that.

### 8.7 The `gateway` block

`spec.gateway` declares the whole layer. All subfields are OPTIONAL; an absent
`spec.gateway` means "no throttling, identity adapters, metering off" — a bare-box
default (G6).

```yaml
spec:
  gateway:
    rateLimit:                       # hierarchical throttle (§8.4)
      - scope: provider              # provider | rotor | step | tenant
        key: anthropic
        rpm: 50
        tpm: 40000
        on_exceed: queue             # queue | backoff | decline
      - scope: tenant
        key: $.inputs.tenant
        concurrency: 4
    throttle:
      backoff: { interval_ms: 500, rate: 2.0, max_ms: 30000 }   # logical clock (§5.3)
    adapters:                        # normalize every input in / output out (§8.2)
      - { io: in,  from: mcp,      to: internal }
      - { io: out, from: internal, to: anthropic-messages }
    translate:                       # MCP ↔ API ↔ format map (§8.3)
      - { from: mcp,                to: anthropic-messages }
      - { from: anthropic-messages, to: internal }
      - { from: openai-chat,        to: internal }
    metering:                        # cost / budgets / attribution (§8.5)
      meter: frontier-only           # frontier-only | all | none
      local: unmetered               # local calls never proxy the gateway
      budget: { tokens: 200000, credits: 5.00, scope: run }
      on_budget_exceeded: escalate   # → §9.1 budget-exceeded trigger
      attribution: [rotor, step, tenant, space]
      cache_accounting: true         # break usage into input/output/cache_write/cache_read (§8.5)
    prompt_cache:                    # provider prefix caching (§8.6)
      enabled: true                  # honor step-level `config.cache` breakpoints
      default_ttl: ephemeral         # provider TTL class (e.g. Anthropic ephemeral)
```

### 8.8 Reference implementation

glyphh-rotor's `models/` **Slicer** — `decide(body, picked_route)`, the
Anthropic-compatible `/v1/messages` gateway that routes each turn onto the
`local` (free) or `frontier` (metered) lane — is the reference implementation of
rate limiting, the lane split, and the metering point. adaL's `/v1/messages`
proxy is the reference format/protocol translator between provider wire formats
and internal I/O. Attach's `HandlerRegistry {method: handler}` (§7.16) is the
MCP/app adapter table. See [glyphh-integration.md §4](docs/glyphh-integration.md).

---

## 9. Escalation

Escalation is the `local → frontier → human` ladder, and it is the **same
mechanism** as a pause (§7.14): a trigger interrupts, checkpoints, and routes. It
is the **dual of attention** (§10): escalation bounds a rotor's *authority*
(which model), attention bounds its *duration and focus* (how long, on what).

### 9.1 Triggers

| Trigger | Fires when |
| --- | --- |
| `refuse` | A grounded step hit the empty-cell invariant (no grounded filler). |
| `low-margin` | An HDC gate's top-second margin < threshold. |
| `frontier-decline` | `FrontierDeclined(402/403/429)` — credits/role/rate governance decline. |
| `gate-reject` | A schema/assertion/evaluator gate rejected the output. |
| `budget-exceeded` | A loop/run token or credit budget was exhausted. |

### 9.2 The ladder

1. **Local (default rung).** Free llama-server propose. Local inference **never**
   proxies the frontier gateway.
2. **Frontier (metered middle rung).** The bundled turn is forwarded verbatim to
   the governed gateway; **credits burn only here** — the metering point. A
   `frontier-decline` is governance, not transport: it **falls back to local** so
   the user keeps working.
3. **Human (top rung).** Surface the refusal ("I don't know.") via a `wait`
   interrupt, or hand to a human queue. This is the empty-cell / low-margin
   terminal.

An `escalate` step (§7.15) declares the `trigger`, the target rung `to`, and the
`fallback`. A down or declined frontier **MUST** degrade to the fallback, never
crash the run.

---

## 10. Attention

**Attention is the dual of Escalation (§9).** Escalation climbs the *model
ladder* — `local → frontier → human` — when a trigger fires; attention governs
*how long the rotor rotates* around the stator and *how strongly the document's
signals are weighted*. Escalation asks "when is this model not enough?" Attention
asks "how long do we keep going, and what do we focus on while we do?" The two
bound a rotor's stochastic work from opposite sides — escalation bounds
**authority**, attention bounds **duration and focus**.

Attention is precisely **two** things: a **budget** (§10.1) and a set of
**weights** (§10.2).

### 10.1 Duration / budget — how long a rotor rotates

The attention **budget** bounds how long a rotor may spend rotating around the
**stator** — the fixed control structure (the declared step graph and its
`select_next` router, §5.1) it iterates against. A budget is expressed along one
or more axes:

- **wall-clock time** — a duration bound. Wall-clock is *measured*, but — per the
  determinism rule (§6.2) — it is **never a control predicate**: the budget is
  recorded as a duration and the *exhaustion event* is checkpointed at the tick it
  fires (§5.3), so replay reproduces the same stop/escalate decision instead of
  reading a fresh clock.
- **revolutions / iterations** — a count of rotor passes, generalizing a `loop`'s
  `max_iterations` (§7.12) to the whole run.
- **tokens** — a token budget across the run's model calls.
- **cost** — a credit/spend budget, sharing the gateway's metering (§8.5).

When the budget is exhausted the rotor **MUST** take its declared `on_exhausted`
action:

- `stop` — halt at the current terminal;
- `escalate` — hand up the ladder (§9);
- `best-effort` — emit the best result produced so far.

Attention is where a **run-wide** budget lives, above any single `loop` (§7.12):
a loop's `budget` bounds one iteration cluster; the attention budget bounds the
entire rotation. Exhaustion is the same event the `budget-exceeded` escalation
trigger (§9.1) names.

### 10.2 Signal weighting — what the rotor attends to

The second half of attention is how strongly the **signals in the rotor
document** are weighted. Steps, signal references, and context carry **weights**
that state what the rotor attends to and how strongly:

- A **higher-weighted signal gets more focus and holds the rotor's focus
  longer** — it resists being dropped from the composed context and biases the
  rotor to keep rotating on it before terminating.
- Weights **bias the router's choice of where context flows next.** At each
  `select_next` (§5.1), among eligible successors and context fillers, higher
  weights steer routing and context assembly toward the attended signals.

Weights are **soft biases on the control plane's inputs — not new control
edges.** They *order and prioritize* eligible choices; they never invent a
transition the graph does not declare. This is what keeps attention consistent
with determinism (§6.1): a weight is a static, recorded part of the definition
(or a value derived from recorded state), so `select_next` stays a pure function
of recorded state. A weight **MUST NOT** be read from an un-recorded live source
(§6.2).

### 10.3 Interaction with the router / stator

The metaphor is load-bearing: the **stator is fixed; the rotor rotates around
it.**

- **Budget bounds rotation.** The attention budget (§10.1) caps the number of
  revolutions — and the time, tokens, and cost they consume — before the rotor
  must stop, escalate, or emit best-effort. It is the outer bound on how many
  times control may circle the stator.
- **Weights bias routing.** The attention weights (§10.2) do not change the
  stator's structure — the declared edges are unchanged — they only *order the
  choices within it*, pulling `select_next` toward higher-weighted signals and
  holding attended context longer.

Escalation and attention pair cleanly:

| | Escalation (§9) | Attention (§10) |
| --- | --- | --- |
| **Governs** | model authority | rotation duration + signal focus |
| **Axis** | `local → frontier → human` | budget (time/revolutions/tokens/cost) + weights |
| **Fires on** | refuse / low-margin / decline / gate-reject | budget exhaustion; weight-biased routing |
| **On exhaustion** | surface refusal (human rung) | `stop` / `escalate` / `best-effort` |

### 10.4 The `attention` field

`attention` is declared at the document level (`spec.attention` — the run-wide
budget + default weights) and **MAY** be refined per step (a critical `gate` gets
a high weight; an expensive `model` gets a tighter token sub-budget). A step's
`attention` overrides the document default for that step.

```yaml
spec:
  attention:
    budget:                          # how long the rotor may rotate (§10.1)
      wall_ms: 120000                # recorded duration, not a live-clock predicate (§6.2)
      revolutions: 6                 # max rotor passes / loop revolutions
      tokens: 150000
      cost: 3.00                     # credits — shares gateway metering (§8.5)
      on_exhausted: best-effort      # stop | escalate | best-effort
    weights:                         # signal weighting — what the rotor attends to (§10.2)
      $.inputs.entity: 1.0           # the entity signal is held longest
      $.steps.recall.rows: 0.8       # grounded facts weighted high
      $.steps.semantic.hits: 0.3     # semantic recall weighted lower
```

A per-step override:

```yaml
- id: verify
  type: gate
  attention:
    weights: { $.steps.reason.text: 0.9 }   # focus the router on the grounded verdict
    budget: { revolutions: 3 }               # re-enter at most 3× before escalating
  # ... config as in §7.8
```

---

## 11. Identity & Principals

Identity, security, governance, and assurance (§11–§14) are the **trust layer**
that makes a rotor safe to run inside an organization. One principle governs all
four, and it is a direct consequence of portability (G5):

> **A rotor DECLARES what it requires; the platform (on behalf of a principal)
> GRANTS the actual policy; the runtime ENFORCES at execution — deny-by-default. A
> rotor can never self-grant.**

The declaration is **portable** and lives in the document (`spec.identity`,
`spec.policy`, `spec.access`, `spec.assurance`, §4.1). The grant is **external,
org-owned, and MUST NOT appear in the portable document** — it lives in the
platform's policy store, keyed to the principal and the org. Enforcement happens in
the run loop and at the gateway (§8), where each declared requirement is checked
against the granted policy before any effect fires. A requirement without a
matching grant is **denied**, never assumed.

### 11.1 Two identities per run

Every run executes under **two** identities, and both are recorded (§5.4):

- **Principal** — the authenticated caller on whose behalf the run executes: a
  human user or a service account. The principal is established by the platform's
  device-auth session / JWT — the glyphh **device-auth + license** flow is the
  reference control — never by the document. It carries the **scopes** and
  **grants** the run may draw on.
- **Agent identity** — the rotor instance itself: the unit of attribution, audit,
  and inter-rotor calls. It answers "which rotor, at which version, in which run,
  did this?" and is derived from `(namespace/name@version, run_id)`. It is what a
  callee sees when one rotor calls another (§11.3).

The principal answers *who authorized this*; the agent identity answers *what
acted*. Because a `StepRecord` (§5.4) carries **both**, every effect in the event
history is attributable to a caller **and** to a rotor instance — the audit trail
governance (§13) and assurance (§14) report against.

### 11.2 The `identity` declaration

A document **DECLARES** the principal kind and the scopes it requires; it does not
name a concrete principal (that would not be portable) and it cannot grant itself
any scope:

```yaml
spec:
  identity:
    principal:
      required: true                 # a run MUST execute under an authenticated principal
      kind: [user, service]          # acceptable principal kinds
    scopes:                          # scopes the rotor REQUIRES to run
      - memory:read:relational
      - connection:crm:read
      - model:frontier:invoke
    on_missing_scope: refuse         # refuse | escalate — deny-by-default
```

At run start the platform authenticates the principal, resolves the **granted**
scopes for that principal in that org, and binds them to the run's **identity
context**. If a required scope is not granted, the run is denied per
`on_missing_scope` — it never proceeds with an assumed scope. The granted scope set
is checkpointed once at run start, so replay sees the **same authority** the
original run saw; a re-grant or revocation after the fact **MUST NOT** change a
recorded run's decisions (§6.1).

### 11.3 Identity attenuation across sub-rotors & handoffs

A `sub-rotor` call or `handoff` (§7.19) **attenuates** identity: the callee runs
under the **same principal**, but with a scope set that is the **intersection** of
the caller's granted scopes and the callee's declared requirement. A callee can
**never** exceed the caller's grants — attenuation is monotonic downward, the
capability-security rule.

- `mode: call` — the callee's agent identity is distinct (its own
  `name@version`); the principal is inherited; scopes are intersected. The callee's
  effects are attributed to the callee's agent identity **and** the shared
  principal.
- `mode: handoff` — control transfers; the successor still runs under the same
  principal with attenuated scopes. A handoff **MUST NOT** re-broaden scope.

An engine **MUST** refuse a sub-rotor whose declared requirement exceeds the
caller's granted scopes (a `gate-reject` / refuse, §9.1) rather than silently
narrowing the work — the mismatch is surfaced, not hidden.

---

## 12. Security

RotorSpec's security model is **deny-by-default** (§11) and is built around one
observation: a rotor mixes trusted control (the document) with **untrusted data**
(model output, tool results, connection content, retrieved memory). The controls
below keep untrusted data from ever becoming control.

### 12.1 Threat model

| Threat | Vector | Required control |
| --- | --- | --- |
| **Prompt injection** | Instructions smuggled inside connection content or stator memory that the model then obeys | Instruction-source boundary (§12.2) + input firewall gate (§14.2) |
| **Secret exfiltration** | Credentials leaking into the model context, a frame, or an output | Secrets isolation (§12.3) |
| **Excess authority** | An effect step doing more than the task needs | Effect sandboxing (§12.4) + control policy (§13.1) |
| **Data over-exposure** | A rotor reading memory / connection fields it was never granted | Data-access grants + redaction (§13.4) |
| **Ungrounded assertion** | The model asserting a fact memory did not return | HDC ground gate as an output anomaly (§14.2) |
| **Transport tampering / unmetered egress** | Raw wire calls bypassing governance | The gateway is the only egress (§8, §12.5) |

### 12.2 The instruction-source boundary

This is the load-bearing security invariant:

> Content ingested from a **connection** (§13.4) or read from the **stator**
> (retrieved memory, prior turns, tool results) is **DATA, never INSTRUCTIONS**. It
> **MUST NOT** influence control flow.

RotorSpec enforces most of this **by construction**, and the rest by rule:

- **Control flow reads only recorded state via typed predicates.** A `branch`
  (§7.11) evaluates *pure predicates over typed Context values* — it cannot execute
  text found in a connection payload. Retrieved content is a **value** in Context,
  not a step to run; there is no "eval the tool result" primitive.
- **No model-generated control.** `retrieve.sql` forbids model-generated SQL
  (§7.5); `plan` constrains decode to schema enums (§7.10). A model proposes
  *values*, never the next transition.
- **Ingested content is quarantined and scanned.** Prompt text and any
  connection/stator content flowing into a `model` step **SHOULD** pass an **input
  firewall gate** (§14.2) — the glyphh **firewall-scanner** is the reference
  control — which flags injection / jailbreak patterns as an **INPUT anomaly**
  before the content reaches the model. An anomaly is a first-class signal (§14.2),
  routable to refuse or escalate — not a log line.

A rotor that lets connection or stator content pick the next step is **malformed**
under this spec, not merely risky.

### 12.3 Secrets isolation

Connection credentials — API keys, OAuth tokens, connector headers — are
**gateway-held and write-only** (the glyphh custom-MCP / connector model is the
reference: headers are stored encrypted, write-only, and executed server-side):

- A credential **MUST NOT** enter the model context, a prompt block, a frame, a
  `StepRecord` `output`, or any rotor-visible Context value. A rotor references a
  connection **by name and scope**; the gateway attaches the actual secret at the
  transport boundary (§8.2) and strips it from everything that returns.
- A `tool` / `model` / `escalate` step names the connection it uses; it never
  carries the secret. Secrets live on **one side** of the gateway; rotor-visible
  state lives on the other.
- An engine **MUST** redact any credential-shaped value that would otherwise be
  checkpointed, so the event history can be shared for replay/audit without leaking
  secrets.

### 12.4 Effect sandboxing

Every effectful step (`model`, `tool`, `write`, `escalate`, side-effecting
`sub-rotor`) runs **least-privilege, deny-by-default**:

- A step may invoke only the tools, models, and connections its **granted** policy
  allows (§13.1). An un-granted effect is **denied**, not attempted.
- The effect runs with only the data-access scopes granted to the run (§13.4) — it
  cannot read a memory space or connection field it was not granted, even
  transitively through a sub-rotor (§11.3).
- `tool.app` dispatch is **loopback-only** (§7.16) — nothing dials into a personal
  machine — and is bounded to the declared `HandlerRegistry` methods.

### 12.5 Transport security is the gateway

There is exactly one egress: the **gateway** (§8). Every effectful boundary — a
`model` call, an `escalate` call, `tool` dispatch, a `retrieve.vector` embedding —
crosses it (§8, §8.5). A rotor **MUST NOT** open a raw wire call that bypasses the
gateway, because that would bypass secrets isolation (§12.3), metering (§8.5),
routing governance (§13.3), and the anomaly gates (§14.2) at once.
Deterministic-given-store steps never leave the box and never touch the gateway
(§8) — they have no transport-security surface.

---

## 13. Governance

Governance is the **control plane over the trust layer** (§11): the org-owned
policy that decides what a rotor is *allowed* to do. Per the governing principle
(§11), the policy is a **grant** — external, org-owned, and **MUST NOT** live in
the portable document. The document only **declares** the surface it needs
(`spec.policy`, `spec.access`, §4.1); the platform grants the allow/deny decision;
the runtime and gateway (§8) enforce it, deny-by-default. Governance has four
parts.

### 13.1 Control policy — allow / deny + approval gates

The org grants **allow/deny-lists** over the primitives a rotor may use:

- **step types** (e.g. deny `tool.app` in a headless rotor),
- **tools** (which `tool.mcp` methods, which `tool.app` methods),
- **models** (which model ids / lanes — see routing, §13.3),
- **connections** (which connectors at all, before field scoping in §13.4).

A denied primitive is a `gate-reject` / `FrontierDeclined`-class governance decline
(§9.1) — it routes to a fallback or refusal, never a crash. Control policy also
grants **required human-approval gates**: the org can require that a given effect
(publish, send, spend above a threshold) pass a human `approval`. These **compose
with the existing `gate` (§7.8, `mode: approval`) and `wait` (§7.14) primitives** —
governance does not add a new step type; it *requires* one the author must include,
and the platform verifies its presence. glyphh's **capabilities / guardrails** model
is the reference: a rotor's declared capabilities are matched against the granted
guardrails at load.

The document declares the surface; the external grant decides:

```yaml
spec:
  policy:
    requires:                        # what the rotor DECLARES it will use
      step_types: [model, retrieve.sql, gate, tool]
      tools: [think, query, recall]
      models: [glyphh-local, claude-sonnet]
      connections: [crm]
    approval_gates:                  # effects the rotor expects to be gated
      - on: publish
        gate: approval               # composes with §7.8 / §7.14
    # the actual allow/deny GRANT is external (org policy store), NOT here
```

An engine **MUST** deny at load any rotor whose declared surface exceeds the
granted policy, and **MUST** deny at run any effect the granted policy does not
allow. A rotor cannot widen its own policy (§11).

### 13.2 Token / budget governance across all rotors

Metering (§8.5) and the attention budget (§10.1) bound **one run**. Governance adds
the **aggregate** bound: a **budget hierarchy** spanning every rotor an org runs.

> **run-level attention budget (§10.1) ⊂ per-rotor budget ⊂ tenant/org cap
> (spanning every rotor).**

Each level is a ceiling on the one below:

- the **attention budget** caps a single run's rotation (time / revolutions /
  tokens / cost, §10.1);
- a **per-rotor budget** caps the aggregate spend of all runs of one rotor
  version;
- a **tenant/org cap** caps aggregate spend across **every** rotor the org runs —
  the FinOps ceiling. glyphh's **credits / metering** ledger is the reference.

The org cap is a **grant** (external), not a document field. When it is exhausted,
the gateway's `budget-exceeded` trigger (§9.1) fires for **every** rotor drawing on
that cap — all of them **throttle, stop, or escalate** per the org's declared
`on_org_cap_exhausted` policy — not just the one run that happened to cross the
line. This is **aggregate FinOps governance**, distinct from a single run's
attention budget: attention bounds *this rotation*; the org cap bounds *the whole
tenant*. Because the exhaustion event is checkpointed at the tick it fires (§8.5,
§10.1), replay of any affected run reproduces the same throttle/stop/escalate
decision.

### 13.3 Routing governance — where a principal MAY route

Escalation (§9) and the router pick *a* lane; **routing governance** grants *which*
lanes / models / providers / rotors a principal is **allowed** to route to:

- **PII → local-only.** A run handling PII is granted `model:local` only; the
  gateway **MUST** deny a `frontier` route for it, so PII never leaves the box.
- **approved-providers-only.** An org grants a specific set of frontier providers;
  a route to any other provider is a `frontier-decline` governance decline (§9.1).
- **rotor allow-list.** Which `sub-rotor` refs a principal may call (§11.3
  attenuation still applies on top).

**The gateway enforces; the router obeys.** The router (§5.1, `select_next`) may
only *propose* a lane; the gateway (§8) checks it against the granted routing policy
and denies a disallowed route before the call leaves the box. Routing governance is
therefore a gateway concern (transport security, §12.5), enforced at the single
egress.

### 13.4 Data-access governance — deny-by-default grants

What a rotor **can and cannot see** is the most consequential grant. It is
**deny-by-default**: a rotor sees **only** what it is granted, and grants are
**scoped**.

**From a connection** — scoped to **connector → operation → field**. Field- and
row-level scoping is the **floor**, not a nice-to-have: a grant names not just "the
CRM connector" but which operations (read vs write) and which **fields / rows** are
visible. glyphh's **connector governance + org_roles** model is the reference.

**From the stator** — scoped to which **memory spaces / entities / roles** a rotor
may **read** and **write**. A grant states, e.g., that a marketing rotor may read
the `marketing` space's `relational.*` roles but **cannot see the `finance` space**
at all.

**Redaction happens before content enters the rotor's context.** Ungranted fields
are **removed at the gateway / retrieval boundary** — the rotor never receives them,
so they cannot leak into a prompt, a frame, or an output. This is stronger than
filtering after the fact: redaction is upstream of the `model` step, so an ungranted
field is not merely unused, it is **absent**.

```yaml
spec:
  access:                            # what the rotor DECLARES it needs (portable)
    connections:
      - connector: crm
        operations: [read]
        fields: [account.name, account.tier]   # field-level floor
    stator:
      read:
        spaces: [marketing]
        roles: [relational.subject, relational.object]
      write:
        spaces: [marketing]
    # the marketing rotor never declares — and can never be granted — finance
    on_ungranted: redact             # redact | refuse
```

The principal's **granted** scopes (§11.2) flow into the run; the retrieval and
gateway boundaries intersect every read with those grants and **redact the rest
before it reaches the rotor's context**. The document declares the required access;
the platform grants the actual scopes; a field outside the grant is redacted or the
step refuses — deny-by-default (§11). An engine **MUST NOT** admit ungranted
connection or stator content into any rotor-visible value.

---

## 14. Assurance & Observability

Assurance turns a rotor's behavior into **measurable signals** — for the
evaluator-optimizer loop (§7.12), for governance reporting (§13), and for
operators. It is configured (portably) via `spec.assurance` (§4.1); the signals are
emitted as **frames** (§2) and telemetry and recorded alongside the `StepRecord`
(§5.4).

### 14.1 Prompt effectiveness

Every step **SHOULD** emit a small, standard set of **effectiveness metrics** as
telemetry frames:

| Metric | Meaning |
| --- | --- |
| **gate-pass rate** | fraction of `gate` verdicts that `pass` (vs fail / escalate) |
| **escalation rate** | fraction of turns that climbed the ladder (§9) |
| **cost-per-success** | metered spend (§8.5) per grounded / asserted outcome |
| **evaluator quality score** | the `loop` evaluator's score (§7.12) |

These are computed from data the run already records — gate verdicts (§7.8),
escalation triggers (§9.1), `usage` / cost (§8.5), evaluator scores (§7.12) — so
effectiveness telemetry adds **no** new control-flow input and is
determinism-neutral (§6.1). The metrics feed two consumers: the
**evaluator-optimizer `loop`** (§7.12), which uses the evaluator score to decide
iterate-vs-stop, and **observability**, which aggregates them per rotor / step /
principal / tenant for governance (§13) and FinOps (§8.5).

### 14.2 Anomaly detection — a first-class signal

Anomaly detection is expressed as **gate / guardrail types** (composing with §7.8),
not a separate subsystem, and it emits telemetry. There are two directions, and
crucially **an anomaly is a first-class signal that can trigger escalation (§9), not
a log line.**

**INPUT anomaly (the firewall).** Before content reaches a `model` step, an input
gate (`gate` `mode: firewall`) scans the prompt **and** any connection / stator
content (§12.2) for injection / jailbreak / policy-violating patterns. The glyphh
**firewall-scanner** is the reference control. A hit is an **INPUT anomaly** that
routes to `refuse` or `escalate` (§9) — the same accept/reject/escalate contract as
any gate.

**OUTPUT anomaly.** After a `model` step, an output gate (`gate` `mode: anomaly`)
scans the response for:

- **policy / PII violations** (leaking data, disallowed content);
- **drift / out-of-distribution** output (a response unlike the rotor's grounded
  norm); and — the load-bearing case —
- **ungrounded assertion.** *An ungrounded assertion **IS** an anomaly.* When the
  model asserts a fact the entity-keyed memory did not return, the **HDC ground
  gate** (§7.8 `mode: hdc-ground`, §6.3) turns "the model asserted what memory did
  not return" into a **refuse**. Groundedness failure is not a soft warning — it is
  an output anomaly with a deterministic verdict (§6.3), independent of the model
  that produced the assertion.

Because anomalies are gate verdicts, they route through the existing machinery: an
INPUT or OUTPUT anomaly **MAY** fire an escalation trigger (`gate-reject`,
`low-margin`, or `refuse`, §9.1), pausing / checkpointing and climbing the ladder
rather than being swallowed. The anomaly, its type, and its score are emitted as a
frame and recorded in the `StepRecord` (§5.4), so observability (§14.1) and
governance (§13) can report anomaly rates per rotor / principal / tenant.

```yaml
spec:
  assurance:
    effectiveness:                   # §14.1 — emit standard metrics as telemetry
      metrics: [gate_pass_rate, escalation_rate, cost_per_success, evaluator_score]
    anomaly:
      input:                         # §14.2 — the firewall
        mode: firewall               # scan prompt + connection/stator content
        on_anomaly: escalate         # refuse | escalate — a first-class signal
      output:
        mode: anomaly                # policy / PII + drift / OOD
        on_anomaly: escalate
        ground:                      # an ungrounded assertion IS an anomaly (§6.3)
          mode: hdc-ground
          on_ungrounded: refuse
```

An anomaly gate is a `gate` (§7.8): deterministic-given-store where it wraps the HDC
check, and a checkpointed scan otherwise. Either way its verdict is recorded, so
replay reproduces the same accept/reject/escalate decision (§6.1).

---

## 15. Composition

### 15.1 The base rotor

The default rotor for most tasks is `ask → plan → execute → test`:

- **ask** — ingest the prompt; the per-prompt macro cycle
  `WRITE → RECALL → REASON` grounds it (write the turn, retrieve grounding,
  compose a bounded block).
- **plan** — decide the approach: a `branch`/`plan` step, or a sub-rotor handoff.
- **execute** — do the work: `model` / `tool` / `plan`-execute steps.
- **test** — a `gate` verifies the result (HDC ground, schema, or evaluator);
  on reject, `loop` refine or `escalate`.

RotorSpec ships this as `glyphh/base@0.1.0`. It is the fallback for any task with
no domain rotor.

### 15.2 Domain rotors

A team declares pragmatic rotors per task class — web dev, slide (`pptx`)
generation, prestige marketing campaigns, "corp-data → slim pre-trained model
with a prompt call first and last." Each is the same document shape; each may
pin its own `space`, its own step graph, and its own escalation policy. Domain
rotors compose the base rotor's grounding cycle rather than reinventing it.

### 15.3 Sub-rotors & handoffs

A `sub-rotor` step (§7.19) calls a rotor with typed inputs/outputs. Composition
rules:

- The callee declares its own `inputs`/`outputs`; the caller maps them.
- `space: inherit` reuses the caller's `space_id`; a callee with a different
  space **MUST** re-encode at the boundary (cross-space binding is refused).
- `mode: call` returns control to the caller; `mode: handoff` transfers control
  and the caller does not resume.
- One step's frame stream feeds the channel the next step reads (attach `run`
  handler semantics) — rotors compose by piping frames.

### 15.4 The space invariant

Every retrieval/gate/encode step carries a `space_id =
sha256(vector_dim, encoder_seed, roles_config)`. `assert_space()` refuses to bind
across mismatched spaces ("would return noise"). A Full-conformant document
**MUST** declare `spec.space` and every vector step **MUST** inherit or pin a
matching `space_id`. A spec that omits space identity is unexecutable.

---

## 16. Versioning

### 16.1 Two version axes

- **`apiVersion`** — the spec/API version (`rotor.glyphh.ai/vMAJOR.MINOR`).
  Selects execution semantics.
- **`metadata.version`** — the rotor **document** semver. Runs pin to it.

### 16.2 Additive-optional rule (CloudEvents discipline)

New **optional** fields **MAY** be added to a minor spec version without a major
bump. Only breaking or newly-**required** changes bump the major version. This
keeps the ecosystem additive. A published, mechanical **schema migration file**
(OpenTelemetry-style) accompanies each spec version so consumers migrate across
versions programmatically.

### 16.3 Run-pinning & patch gates

A run records the `definitionVersion` it started on. Editing a rotor while runs
are in flight is a determinism hazard — a changed graph breaks replay. RotorSpec
requires:

- **Run-pinning:** an in-flight run finishes on its original definition.
- **Patch gates:** a `patch(marker)` inserts a version marker into the event
  history; on replay a mismatched marker fails the task rather than silently
  diverging. New runs use the updated definition; version-gated code paths let
  both coexist (Temporal patch model).

Without run-pinning and patch gates, "replayable" is a broken promise the moment
the rotor is edited.

---

## 17. Runtime, Scaling, Pooling & Affinity

Everything so far describes what a rotor *is* (§4) and how one **run** executes
(§5–§14). This section is the **operational contract**: how a conformant rotor
**runtime** is deployed, scaled, pre-warmed, and routed so runs are served
efficiently at scale — without ever changing what a run records. It is the one
place the spec speaks about *instances* rather than *runs*, and every guarantee it
adds is **determinism-neutral** (§17.6) by construction.

### 17.1 The runtime instance model

A **rotor runtime** is a self-contained **instance** of this specification: it
executes the run loop (§5), speaks the gateway (§8), enforces the trust layer
(§11–§14), and reads/writes the stator. Three properties make it a clean
horizontal-scaling citizen:

- **Stateless compute.** The instance holds no run-critical state. Everything a
  run needs to advance — the Context (§5.2), the append-only event history of
  `StepRecord`s (§5.4), the HDC store, and the caches (§5.7) — lives in the
  **stator (memory) backend** (§10.3; glyphh's `MemorySubstrate`), not in the
  instance's process memory.
- **All state in the stator.** The event history is the single source of truth
  (§5.4); an instance is a stateless evaluator over it. A run's authority is the
  grant set checkpointed at run start (§11.2), also in the record — not in the
  instance.
- **The gateway is the single transport boundary.** Every effectful I/O crosses
  the one gateway (§8, §12.5); an instance opens no side channel of its own.

**The fungibility invariant.**

> No run-critical state lives in a rotor instance. Any conformant instance **MAY**
> serve any run — including one another instance started — by loading the run's
> pinned `definitionVersion` (§16.3) and continuing (or replaying, §5.4) from the
> recorded event history in the stator. Instances are **fungible**.

This is exactly the **Kubernetes per-instance model**: a rotor runtime is a
**Deployment** of interchangeable **rotor pods**, scaled **horizontally**; a load
balancer **MAY** route any request to any pod; a pod that dies is replaced and the
run continues on its successor, because the state was never in the pod.
glyphh-rotor's `Runtime` — nine lazily-constructed, gracefully-degrading
subsystems (glyphh-integration §1) — is the reference instance; its `store` /
`memory` subsystems are the **stator backend** that outlives any single pod.

The instance is **not** entirely without local content — a loaded local model and
warm caches *do* live in it — but that content is **reconstructible optimization,
never run-critical state**: any instance can rebuild it from the stator and the
model registry. Managing that reconstruction cost across a fleet is the job of
pools.

### 17.2 Instance pools & states: hot / warm / cold

Fungible instances are managed as a **pool**. Each instance is in one of three
states, distinguished by *what has been paid for ahead of the request*:

| State | What is already paid | Serves a matching run |
| --- | --- | --- |
| **Hot** | model loaded, connections open, caches primed | **immediately** — no cold-start on the request path |
| **Warm** | loaded but idle / suspended | **fast resume** — cheaper than cold, not instant |
| **Cold** | not instantiated | pays the **full cold-start** |

**Hot — the contract.** A **hot** instance **MUST** be able to begin serving a
matching run *immediately*, with no cold-start work on the request path. "Hot"
**GUARANTEES** all of:

- the **local model is loaded** — the dominant cold-start cost: the GGUF weights
  are resident in RAM/VRAM and `llama-server` is ready to decode (§7.2;
  glyphh-integration §4);
- **stator connections are open** — the memory/store backend (SQLite or
  Postgres + pgvector) is connected and the run's `space_id` validated (§15.4);
- **caches are primed** — the result cache (§5.7) is reachable and any stable
  provider prompt-cache prefixes (§8.6) are established;
- **gateway connections are established** — provider / tool transport channels are
  open (§8.2).

An idle hot instance **costs money** — reserved compute, pinned VRAM — which is
precisely why warmth is **governed**, not free (§17.3).

**Warm** — the process (and often the model) exist but are paused: a Fly Machine
`suspend`/`resume`, a replica scaled to a parked minimum, a checkpointed local
process. Resume restores the hot guarantees far more cheaply than a cold-start,
but not instantaneously.

**Cold** — not instantiated: the first request pays the **full cold-start** —
schedule a pod, pull the image, load the model, open connections, prime caches.

The three states are a **portable contract, not an implementation.** Whether
"hot" is realized as a Kubernetes min-warm replica, a Fly Machine kept resumed, or
a long-lived local `llama-server` process, the guarantee above is identical — so a
rotor's warmth declaration (§17.3) means the same thing on every runtime.

### 17.3 Pre-warming: the `pool` block

A rotor that expects demand **SHOULD** pre-warm against it: *"we expect a burst of
requests for question class 'a' between 08:00 and 18:00 — have hot instances
ready so the first callers don't each eat a cold-start."* Consistent with the
spec's **declare → grant → enforce** discipline (§4.1, §11), warmth is *declared,
not commanded*: a rotor **DECLARES** its warmth *intent* in a `pool` block; the
platform / runtime **PROVISIONS** the actual pool against real demand and real
budget.

```yaml
spec:
  pool:
    minHot: 3                 # keep ≥3 hot instances ready at all times
    maxHot: 20                # never warm more than 20 (a hard cost ceiling)
    targetConcurrency: 5      # in-flight requests per instance before warming another
    warm:
      schedule: "mon-fri 08:00-18:00"   # when to hold minHot warm — or: predicted
    coldStart:
      budget_ms: 8000         # tolerated cold-start; a class that would exceed it SHOULD be pre-warmed
```

- **`minHot` / `maxHot`** — the floor and ceiling on hot instances. `minHot`
  trades idle cost for zero cold-starts on the hot path; `maxHot` is a hard cost
  ceiling on how far the pool may warm.
- **`targetConcurrency`** — the in-flight requests one instance handles before the
  runtime warms another. This is the autoscaling signal, expressed portably
  (requests-per-instance), independent of the mechanism that acts on it.
- **`warm.schedule`** — when to hold `minHot` warm: a recorded schedule window, or
  `predicted` (the runtime's autoscaler drives warmth from observed / forecast
  demand rather than a fixed calendar).
- **`coldStart.budget_ms`** — the tolerated cold-start latency; a class of request
  whose cold-start would exceed it **SHOULD** be pre-warmed rather than served
  cold.

**Warmth is DECLARED intent; the platform PROVISIONS the pool.** A rotor cannot
pin actual capacity — `pool` states what the rotor *wants*; the runtime decides
what it *gets*, against live demand and, critically, against budget. This mirrors
`spec.identity` / `spec.policy` (§4.1): the document declares a requirement, the
platform confers the reality, and a rotor can never self-grant.

**Warm-pool size is BOUNDED by §13 budget governance.** An idle hot instance is a
**FinOps cost** — reserved compute, pinned VRAM — exactly like a metered model
call is (§8.5). So *how much* a rotor may pre-warm is **capped by the org's token /
budget governance** (§13.2): a rotor's realized `minHot` / `maxHot` **MUST** fit
within the budget hierarchy `attention budget ⊂ per-rotor budget ⊂ tenant/org cap`
(§13.2). A `pool` whose warmth would exceed the granted budget is **provisioned
down** to what the grant allows — deny-by-default (§11): a rotor can no more
self-grant warm capacity than it can self-grant a scope. When the org cap is
exhausted (the §13.2 `budget-exceeded` condition), **pre-warming is among the
first costs shed** — the runtime scales hot instances toward `minHot`, then toward
`0`, before it throttles live runs. (Local-model warmth burns no *frontier*
credits — local inference never proxies the metered gateway, §8.5 — but the
compute and memory it occupies are still a real, governed cost, so the budget
bound applies to local and frontier pools alike.)

### 17.4 Affinity routing: the `affinity` block

Routing a prompt to compute happens at **two levels**, both through the gateway
router (§5.1 `select_next`, §8):

**1. Rotor-type routing — *which rotor*.** Which rotor should serve a prompt: the
existing semantic cosine / NL router (the §7.11 `SchemaGuard.classify`
nearest-prototype pattern feeding a `branch`, over the §10.3 stator) plus any
declared prompt→rotor affinity rules. This selection is **subject to routing
governance** (§13.3): the gateway **MUST** deny a route to a rotor the principal
was not granted, before the request is dispatched.

**2. Instance affinity — *which warm instance*.** Given the rotor, *which of its
warm instances* should serve this request. The router **SHOULD** prefer a hot
instance whose stator / cache already holds the matching warm state, so warm state
and cache hits (§5.7) are **reused rather than re-primed** — cache-affinity /
session-affinity / sticky routing. A rotor declares the affinity keys:

```yaml
spec:
  affinity:
    keys: [tenant, conversation, entity]   # what makes an instance "already warm" for a request
    mode: prefer                           # prefer (default) | require
```

- **`keys`** — the dimensions along which warm state clusters: a `tenant`'s open
  connections and resolved grants, a `conversation`'s recent context and primed
  prompt-cache prefix (§8.6), an `entity`'s HDC records and prior probes (§7.6).
  The router hashes the request's key values and prefers an instance already warm
  for them.
- **`mode: prefer`** (**DEFAULT**) — a **soft** bias: route to the affine instance
  when one is available and unsaturated, otherwise **gracefully fall back** to any
  instance, or to a cold-start. `prefer` never blocks a request on the affine
  instance's availability.
- **`mode: require`** — a **hard pin**: the request **MUST** be served by the
  affine instance. This is stronger and more dangerous — it **can starve
  throughput when the affine instance is saturated**, because matching requests
  queue behind it instead of spilling to idle capacity. `require` **MUST** carry
  that operational note and **SHOULD** be reserved for correctness-driven pinning
  (state that genuinely cannot move), never used as a performance default.

Affinity is a routing *preference over the pool*. It never changes *which rotor*
runs, nor *what* the run computes (§17.6).

### 17.5 Sub-rotors & pools

A `sub-rotor` call or handoff (§7.19) is itself a run, so it too is served from a
pool. A sub-rotor invocation:

- **MAY route to a warm instance in the sub-rotor's own pool** rather than
  cold-starting one — the handoff is a routing decision over that sub-rotor's
  `pool` / `affinity` (§17.3–§17.4), exactly like a top-level request;
- **propagates the affinity keys through the handoff** — the caller's key values
  (`tenant`, `conversation`, `entity`) flow to the callee's router, so a
  `conversation`-affine chain of sub-rotors lands on instances already warm for
  it.

Crucially, **affinity propagation does not widen authority.** Identity still
**attenuates** across the handoff (§11.3): the callee runs under the caller's
principal with the **intersection** of the caller's grants and its own declared
requirement, *regardless of which instance serves it*. Affinity chooses **where**
the callee runs; attenuation fixes **what it may do** — the two are orthogonal, and
an affine route **MUST NOT** be read as a grant. A warm instance carries no
authority of its own; every effect is still checked against the run's recorded
grant set (§11.2; execution-model §6) before it fires.

### 17.6 Determinism-neutrality

Pooling, warm state, and affinity routing are **runtime optimizations**. Held to
the same discipline as caching (§5.7, §6.1), they **MUST NOT** change a run's
recorded outputs or its transitions:

- **Which instance served a run** is **operational metadata** — recorded for
  observability (§14.1), never a control-flow input. A `branch` (§7.11) **MUST
  NOT** read it; `select_next` (§5.1) **MUST NOT** depend on it.
- **Whether an instance was hot, warm, or cold** changes only *latency and cost*,
  never *output*. A run served cold and the same run served hot record the
  **identical** event history.
- Because all state lives in the stator and the run is pinned to its
  `definitionVersion` (§16.3, §17.1), **replay is independent of the instance**:
  any fungible instance replays the recorded history to the same transitions — the
  engine that replays need not be the one that first ran it.

This is the §6 rule applied to instances: a run is a pure function of its
definition and recorded event history, so *where* and *how warm* it ran are — like
a cache being warm or cold (§5.7, §6.1) — invisible to its result. An engine
**MAY** record the serving instance id and its hot/warm/cold state in telemetry
(§14.1) for FinOps and operations; it **MUST NOT** let either enter a predicate.

### 17.7 Spec boundary & conformance

RotorSpec standardizes the **declaration** and the **behavior**, and stops there:

- **Standardized (portable).** The `pool` and `affinity` **declarations**
  (§17.3–§17.4) and their **semantics** — the hot/warm/cold guarantees (§17.2),
  the pre-warm *declare → provision* contract bounded by budget (§17.3), affinity
  resolution with `prefer` fallback and `require` pinning (§17.4), and
  determinism-neutrality (§17.6). Two runtimes reading the same `pool` / `affinity`
  block **MUST** agree on what it *means*.
- **Runtime-specific (out of the portable document).** The pool **management
  mechanism** — Kubernetes HPA / min-replicas, Fly Machine suspend/resume,
  predictive autoscaling, bin-packing, a load balancer's consistent-hash ring — is
  an engine concern and **MUST NOT** appear in the portable document (G5). *How* a
  runtime keeps `minHot` instances warm, or *how* it hashes affinity keys onto
  pods, is not standardized; *that* it honors the declared guarantees is.

**Conformance.** Pooling and affinity are **runtime-optional optimizations**, not
required semantics. They are **not required at Core (L1) or Standard (L2)**: an
engine that ignores `pool` and `affinity` entirely — cold-starting every run,
routing every request to any instance — is fully conformant at whatever level it
otherwise meets, because determinism-neutrality (§17.6) guarantees it produces
identical runs, only slower. A runtime that **does** implement them **MUST** honor
the declared semantics: the hot contract (§17.2), `prefer` fallback and `require`'s
starvation note (§17.4), and the §13.2 budget bound (§17.3). This is the same
treatment as prompt caching (§8.6) — a portable, determinism-neutral optimization
the document **MAY** declare and a runtime **MAY** realize, never a correctness
requirement (see the §3 conformance note).

---

## 18. Conformance Testing

RotorSpec ships a **golden-transcript conformance suite**: `(rotor definition +
recorded inputs + recorded activity results) → expected control-flow decisions`.
An engine is **Full-conformant** when, replaying each golden transcript, it
reproduces the expected sequence of transitions exactly. This is how RotorSpec
becomes a *standard* rather than one implementation — the CloudEvents / OpenTelemetry
model. The reference executor **glyphh-rotor** is the seed implementation; it is
not privileged by the spec.

---

## Appendix A — Step Type Summary

| `type` | Category | Determinism | Effectful | Result-cacheable (§5.7) |
| --- | --- | --- | --- | --- |
| `prompt` | compose | Deterministic | no | default (pure) |
| `model` | data-plane | Stochastic-checkpointed | yes | opt-in (freezes sample) |
| `hdc.map` | encode | Deterministic-given-store | no | default |
| `write` | memory | Deterministic-given-store | yes (idempotent) | no (idempotency only) |
| `retrieve.sql` | retrieval | Deterministic-given-store | no | default |
| `retrieve.kb` | retrieval | Deterministic-given-store | no | default |
| `retrieve.vector` | retrieval | Stochastic-checkpointed (embed) | no | default |
| `gate` | control | Deterministic-given-store (scan: Stochastic-checkpointed) | no | default |
| `assert` | terminal | Deterministic-given-store | no | default |
| `plan` | data+exec | decode Stochastic / execute Deterministic | no | opt-in (freezes decode) |
| `branch` | control | Deterministic | no | n/a (control) |
| `loop` | control | Deterministic control / Stochastic body | maybe | per body step |
| `parallel` | control | Deterministic fan-in | maybe | per branch step |
| `wait` | control | Deterministic-given-history | no | no (external input) |
| `escalate` | data-plane | Deterministic route / Stochastic call | yes | no (idempotency only) |
| `tool` | effect | mcp Deterministic / app Stochastic | yes (idempotent) | mcp default / app no |
| `transform` | compose | Deterministic | no | default |
| `cascade` | maintenance | Stochastic-checkpointed | yes | no |
| `sub-rotor` | composition | inherits callee | maybe | inherits callee |
| `fail` | terminal | Deterministic | no | n/a (control) |

The **Gateway** (§8), **Attention** (§10), the **trust layer** — Identity
(§11), Security (§12), Governance (§13), Assurance (§14) — and **runtime pooling &
affinity** (§17, `spec.pool` / `spec.affinity`) are cross-cutting layers / fields,
not step types. They govern the transport, rotation, authority, observability, and
*where / how warm* every step executes, and so do not appear in this catalog.
Runtime pooling & affinity in particular change only *which instance* serves a run
and *how warm* it is — never *what* it computes (§17.6). Anomaly detection (§14.2)
and human-approval gates (§13.1) add **modes** to the existing `gate` type
(`firewall | anomaly | approval`), not new step types — keeping the catalog closed
(G2).

The **Result-cacheable** column states each type's *default* cross-run cacheability
(§5.7): `default` = safe to cache by content address; `opt-in` = allowed but off
unless the step declares `cache` (it freezes a stochastic sample); `no` = relies on
within-run idempotency (§5.6) only. `cache: none` disables caching on any step, and
`cache: { scope: … }` widens it. Prompt caching (§8.6, the `config.cache`
breakpoints on `prompt`/`model`) is **orthogonal** to this column — it caches a
prompt *prefix* at the provider, never the step's *result*.

## Appendix B — Reserved terminals & references

- Terminals: `end` (succeed), `__fail__` (typed failure), an `assert`/refuse.
- Context references: `$.inputs.<name>`, `$.state.<key>`, `$.steps.<id>.<out>`.
- Slot keys: `layer.role` from the fixed 7×33 `universal-7x33` schema (§ glyphh
  integration docs).
- Gateway scopes: `provider | rotor | step | tenant`; wire formats: `mcp |
  anthropic-messages | openai-chat | internal`; metering: `frontier-only | all |
  none` (§8).
- Attention budget axes: `wall_ms | revolutions | tokens | cost`; exhaustion:
  `on_exhausted: stop | escalate | best-effort` (§10).
- Result-cache (§5.7): `cache.key: auto | <expr>`; `cache.ttl`: a duration;
  `cache.scope: run | rotor | tenant | global`; `cache: none` disables. `usage`
  cache dispositions (§8.5): `input | output | cache_write | cache_read`.
- Prompt-cache (§8.6): `config.cache.breakpoints` on `prompt`/`model` steps; wire
  forms `anthropic: cache_control{type: ephemeral}` | `openai: auto-prefix` |
  `local: kv-prefix`.
- Identity (§11): `identity.principal.kind: user | service`; scope form
  `resource:action:qualifier` (e.g. `memory:read:relational`,
  `connection:crm:read`, `model:frontier:invoke`); `on_missing_scope: refuse |
  escalate`. `StepRecord` identity fields: `principal`, `agent_identity`.
- Control policy (§13.1): `policy.requires: { step_types | tools | models |
  connections }`; `policy.approval_gates[].{ on, gate: approval }`. The allow/deny
  **grant** is external (never in the document).
- Budget governance (§13.2): hierarchy `attention budget ⊂ per-rotor budget ⊂
  tenant/org cap`; `on_org_cap_exhausted: throttle | stop | escalate`.
- Data-access (§13.4): `access.connections[].{ connector, operations: [read|write],
  fields, rows }`; `access.stator.{ read | write }.{ spaces, entities, roles }`;
  `on_ungranted: redact | refuse`.
- Assurance (§14): effectiveness `metrics: gate_pass_rate | escalation_rate |
  cost_per_success | evaluator_score`; anomaly gate `mode: firewall | anomaly`
  (added to §7.8's `hdc-ground | schema | assertion | evaluator | approval`);
  `on_anomaly: refuse | escalate`; `anomaly.output.ground.on_ungrounded: refuse`.
- Pool (§17.3): `pool.{ minHot, maxHot, targetConcurrency }`; `pool.warm.schedule:
  <window> | predicted`; `pool.coldStart.budget_ms`. Instance readiness states:
  `hot | warm | cold` (§17.2). Warm-pool size is bounded by the §13.2 budget.
- Affinity (§17.4): `affinity.keys: [tenant | conversation | entity | …]`;
  `affinity.mode: prefer | require` (default `prefer`). Runtime-optional and
  determinism-neutral (§17.6); the serving instance id + hot/warm/cold state are
  telemetry only, never a control predicate.
