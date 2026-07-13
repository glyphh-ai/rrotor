# RotorSpec Execution Model

How a rotor actually runs: the deterministic loop semantics, and a worked,
step-by-step trace of the base rotor. This complements [SPEC.md §5–§6](../SPEC.md)
with the operational detail an engine author needs.

---

## 1. The run loop

A **run** is one execution of a rotor against concrete inputs. The engine holds:

- the **rotor definition**, pinned by `definitionVersion`;
- the **Context** — typed shared state + namespaced step outputs;
- the **event history** — an append-only log of `StepRecord`s;
- a **logical step counter** (never wall-clock).

The core loop:

```
identity := authenticate(principal); grants := resolve_grants(identity, org)  # §11 — deny-by-default; grant set checkpointed once
cursor := entry step
while cursor is not terminal:
    step := definition.steps[cursor]
    require_policy(step, grants)                 # §13.1/§13.3 — deny un-granted step type / tool / model / route
    input := resolve(step.in, Context)          # pure: references → values
    input := redact(input, grants)               # §13.4 — strip ungranted connection/stator fields BEFORE context
    if history has a completed record for (step.id, attempt):
        output := record.output                 # REPLAY: return recorded result — cache/scan NOT re-run
    else:
        output := cache_get(step, input)         # CROSS-RUN: warm result-cache hit? (§5.7)
        if output is MISS:
            output := execute(step, input, grants) # data plane; least-privilege (§12.4); secrets stay gateway-side (§12.3)
        append StepRecord(step, input, output, identity)  # CHECKPOINT — records principal + agent_identity (§5.4, §11)
    merge(output → Context, reducers)           # fan-in
    cursor := select_next(step, output, Context) # DETERMINISTIC given recorded state — data never steers (§12.2)
    logical_tick += 1
emit outputs := project(spec.outputs, Context)
```

The **firewall** and **anomaly** scans (§14.2) are not extra lines above — they are
`gate` steps *in the graph* (`mode: firewall` before a `model` step, `mode: anomaly`
after it), so their verdicts are checkpointed and replayed like any gate.
`require_policy` and `redact` are pure functions of the **grant set** — which is
resolved and checkpointed once at run start — so they are replay-stable: a later
re-grant or revocation cannot change a recorded run.

Five invariants make this a *deterministic, governed loop*:

1. **`select_next` depends only on recorded state.** Predicates read Context;
   iteration is by recorded/sorted order; no wall-clock, RNG, or un-recorded live
   read participates.
2. **`execute` is quarantined.** All nondeterminism (model, tool, embedding)
   happens inside `execute`, and its result is checkpointed. On replay, `execute`
   is skipped entirely.
3. **Effects are idempotent.** Each effectful step's idempotency key
   (`sha256(definitionVersion, step_id, canonical(in), space_id)`) ensures a
   retry or replay returns the recorded effect instead of re-firing it.
4. **Caching is subordinate to the checkpoint.** A cross-run result-cache hit
   (`cache_get`, §5.7) is written into the `StepRecord` at first execution *just
   like a fresh result*; on **replay** the top branch fires first, so the cache is
   never consulted again. A warm, cold, or evicted cache therefore cannot change a
   recorded run's transitions. Prompt caching (§8.6) is not even in this loop — it
   changes only the provider bill recorded in `usage`, never `output`.
5. **The trust layer enforces deny-by-default, off recorded grants.** Identity is
   bound and its grant set checkpointed at run start (§11); every step is
   policy-checked (§13.1), its inputs redacted to the grant (§13.4), and its
   ingested content is DATA that `select_next` never reads as control (§12.2).
   Because enforcement reads only the *recorded* grant set (not a live lookup), it
   is a pure part of the deterministic control plane — see §6.

### Instance selection sits *before* this loop, not inside it

The loop above is what one **rotor instance** does once a run is dispatched to it.
*Which* instance runs it is decided **upstream**, at the router / load balancer,
and is **not part of the loop** ([SPEC.md §17.4](../SPEC.md)):

```
request → rotor-type routing (which rotor)  → §13.3 routing governance
        → instance affinity  (which warm instance in the rotor's pool, §17.4)
        → dispatch to a fungible instance    → THE RUN LOOP ABOVE
```

Two things make this clean:

- **The loop is identical on any instance.** Because the instance is stateless and
  all run-critical state lives in the stator ([SPEC.md §17.1](../SPEC.md)), the
  same run advances identically whichever pod serves it — including a *different*
  pod on resume or replay. A load balancer may send the request anywhere; a pod may
  die and its successor continues from the recorded event history.
- **Hot / warm / cold changes only latency.** Whether the chosen instance had its
  model loaded and caches primed (**hot**), was suspended (**warm**), or had to be
  started (**cold**) affects *how fast* the first step runs, never *what* it
  outputs. Affinity (prefer a warm instance already holding matching
  `tenant`/`conversation`/`entity` state) is a routing *preference*, not a
  control input.

So instance selection is **determinism-neutral** ([SPEC.md §17.6](../SPEC.md)),
exactly like caching (invariant 4): the serving instance id and its hot/warm/cold
state are recorded as telemetry for FinOps and ops, but `select_next` never reads
them. A replay need not even run on the instance that first served the run.

## 2. Resolving inputs

`resolve(step.in, Context)` is pure. References:

- `$.inputs.<name>` — run parameters.
- `$.state.<key>` — shared state.
- `$.steps.<id>.<out>` — a prior step's named output.

Resolution never calls a model and never reads outside the Context. If a
reference is missing, the step fails with `E_UNRESOLVED` before executing.

## 3. Merging outputs (fan-in)

A sequential step writes its `out` names into Context under `$.steps.<id>.*` and
any declared `state` keys. When `parallel`/`loop` produce **concurrent** writes
to the same state key, the engine applies the key's declared **reducer**
(`last-write-wins`, `append`, `merge`, `sum`, `max`, `min`, `union`, or a named
sub-rotor). A concurrent write to a key with *no* reducer raises `E_UNMERGEABLE`
— never a silent race. This is why the state schema is mandatory.

## 4. Selecting the next step

`select_next` resolves in this order:

1. If the step is a **`branch`**, evaluate its ordered pure predicates against
   Context; take the first `when` that holds, else `default`.
2. If a **`gate`** produced a verdict, route by `on_pass` / `on_fail` /
   `on_escalate`.
3. If a **`loop`** is iterating, re-enter the body while the gate rejects and
   `iterations < max_iterations` and budget remains; otherwise take
   `on_exhausted`.
4. Otherwise follow the step's `next`.

Attention **weights** ([SPEC.md §10](../SPEC.md)) never add cases to this list —
they *bias the ordering* among already-eligible successors and context fillers,
pulling `select_next` toward higher-weighted signals. Attention **budget**
exhaustion (revolutions / time / tokens / cost) short-circuits the walk to `stop`,
`escalate`, or `best-effort`, and — like a timeout — is checkpointed at the tick
it fires, so replay reproduces the same decision.

Reserved terminals: `end` (succeed), `__fail__` (typed failure), or an `assert`
that refused. Every path is statically guaranteed to reach one.

## 5. Retries, catch, and escalation

- **retry:** on a typed error, the first matching retrier re-executes the step
  after `interval_ms × backoff_rate^(attempt−1)`, up to `max_attempts`. Each
  attempt is a distinct `(step_id, attempt)` in history.
- **catch:** if retries are exhausted (or none match), the first matching catcher
  routes control to its `next`. `FrontierDeclined` routes to a local fallback,
  never a crash.
- **escalate:** an `escalate` step (or a gate's `on_escalate`) moves work up the
  `local → frontier → human` ladder on a typed trigger (`refuse`, `low-margin`,
  `frontier-decline`, `gate-reject`, `budget-exceeded`).

## 6. Where the trust layer enforces

The run loop (§1) has **five enforcement points**, all reading the *recorded* grant
set so they stay inside the deterministic control plane:

| # | Point in the loop | Enforces | SPEC |
| --- | --- | --- | --- |
| 1 | **Run start** — authenticate principal, resolve + checkpoint grants | Identity bound; deny-by-default; missing scope → refuse/escalate | §11 |
| 2 | **Before a step** — `require_policy(step, grants)` | Allow/deny over step type / tool / model / connection; routing (PII→local, approved-providers) | §13.1, §13.3 |
| 3 | **On `resolve` / retrieval** — `redact(input, grants)` | Ungranted connection / stator **fields removed before they enter context** | §13.4 |
| 4 | **Before `execute`** — an input `firewall` gate | INPUT anomaly (injection/jailbreak) on prompt + ingested content → refuse/escalate | §14.2 |
| 5 | **After `execute`** — an output `anomaly` gate | OUTPUT anomaly (policy/PII/drift; **ungrounded assertion** via the HDC gate) → refuse/escalate | §14.2, §6.3 |

Two properties keep this honest:

- **Data never becomes control.** Point 3 strips ungranted content, and
  `select_next` reads only typed Context values via pure predicates — retrieved
  memory or a tool result is a *value*, never a step to run. That is the
  instruction-source boundary ([SPEC.md §12.2](../SPEC.md)) enforced by the loop's
  shape, not by trust in the model.
- **Enforcement is replay-stable.** The grant set is resolved and checkpointed
  once (point 1); points 2–3 are pure functions of it; points 4–5 are `gate` steps
  whose verdicts are checkpointed. So a re-grant, revocation, or a re-scan after the
  fact cannot change a recorded run — the same invariant as caching (§1, invariant
  4).

**Secrets** never appear at any of these points: a step names a connection, and the
gateway attaches the write-only credential at the transport boundary and strips it
before anything returns ([SPEC.md §12.3](../SPEC.md)), so no credential is ever in
`input`, `output`, a frame, or a `StepRecord`.

## 7. Determinism, honestly

The engine promises: reproducible transitions, replayable transcripts, idempotent
effects, and gate-bounded nondeterminism. It does **not** promise identical model
tokens — floating-point drift, batch scheduling, and provider changes make that
best-effort even at temperature 0 with a seed. The **HDC ground gate** is where
real determinism re-enters: its accept/reject verdict is a pure function of
`(entity, role, filler, store-state, space_id)`, independent of which stochastic
model proposed the filler.

Caching changes none of this. A **result-cache** hit (§5.7) is recorded in the
`StepRecord` like any other output and is never re-checked on replay; **prompt
caching** (§8.6) only shifts tokens on the provider bill (recorded in `usage`).
Neither can alter a transition — that is invariant 4.

## 8. Run-pinning & patch gates

A run finishes on the `definitionVersion` it started with. Editing a live rotor
uses **patch gates**: a version marker is written into history; on replay a
mismatched marker fails the task rather than diverging silently. New runs adopt
the new definition; version-gated paths let in-flight and new runs coexist.

---

## 9. Worked trace — the base rotor

Consider `glyphh/base@0.1.0` answering a grounded question. Definition (abridged):

```yaml
apiVersion: rotor.glyphh.ai/v0.1
kind: Rotor
metadata: { name: base, version: 0.1.0 }
spec:
  inputs:
    - { name: prompt, type: string, required: true }
    - { name: entity, type: string, required: true }
  space: { vector_dim: 10000, encoder_seed: 42, roles_config: universal-7x33 }
  state:
    schema: { candidate: {type: string}, answer: {type: string} }
  steps:
    - id: write    # ASK / WRITE
      type: write
      in: { text: $.inputs.prompt }
      out: { written: number }
      config: { mode: absorb, key: $.inputs.entity }
      next: recall
    - id: recall   # ASK / RECALL
      type: retrieve.sql
      in: { person: $.inputs.entity }
      out: { rows: array }
      config: { op: lookup, slot: relational.object }
      next: compose
    - id: compose  # PLAN — bound the context
      type: prompt
      in: { question: $.inputs.prompt, facts: $.steps.recall.rows }
      out: { text: string }
      config: { template: "Answer using ONLY: {{facts}}\nQ: {{question}}", max_tokens: 1200 }
      next: reason
    - id: reason   # EXECUTE — grounded model (micro rotor)
      type: model
      in: { text: $.steps.compose.text, entity: $.inputs.entity }
      out: { text: string, frames: array }
      config:
        lane: local
        ground: { role: relational.object, enforcement: hard, refusal: "I don't know." }
        micro: true
      next: verify
    - id: verify   # TEST — the HDC ground gate
      type: gate
      in: { entity: $.inputs.entity, role: relational.object, filler: $.steps.reason.text }
      out: { verdict: string, margin: number }
      config: { mode: hdc-ground, admit: 0.10, margin: 0.05, on_pass: assert, on_fail: escalate }
    - id: escalate # TEST fail → ladder
      type: escalate
      in: { text: $.steps.compose.text, reason: $.steps.verify.verdict }
      out: { lane: string, text: string }
      config: { trigger: low-margin, to: frontier, fallback: local }
      next: verify
    - id: assert   # TERMINAL
      type: assert
      in: { filler: $.steps.reason.text, verdict: $.steps.verify.verdict }
      out: { text: string }
      config: { refusal: "I don't know.", empty_cell: refuse }
      next: end
  outputs:
    - { name: answer, from: $.steps.assert.text }
```

### Run: `entity = "sky"`, `prompt = "What color is the sky?"`

| tick | step | input (resolved) | execute (data plane) | StepRecord output | next |
| --- | --- | --- | --- | --- | --- |
| 0 | `write` | text="What color is the sky?" | enricher → `{perceptual:{color:?}}`; absorb keyed `sky` | `{written: 1}` | recall |
| 1 | `recall` | person="sky", op=lookup, slot=relational.object | closed-op SQL over `fact_slots` | `{rows:[{color:"blue"}]}` | compose |
| 2 | `compose` | question + facts | pure template | `{text:"Answer using ONLY: color=blue\nQ: What color is the sky?"}` | reason |
| 3 | `reason` | text, entity="sky" | **stochastic**: micro rotor proposes "blue"; hard gate masks vocab to grounded `(sky, perceptual.color)` continuations | `{text:"blue", frames:[propose,dispose,assert]}` | verify |
| 4 | `verify` | entity="sky", role, filler="blue" | **deterministic-given-store**: `verify(sky, perceptual.color, "blue")` → grounded, margin 0.41 > 0.05 | `{verdict:"pass", margin:0.41}` | assert (on_pass) |
| 5 | `assert` | filler="blue", verdict="pass" | assert grounded filler | `{text:"blue"}` | end |

**Output:** `answer = "blue"`.

### The same run, with the model wrong

Suppose at tick 3 the model proposes "green" (and enforcement were `soft`, so it
slips through generation):

| tick | step | execute | output | next |
| --- | --- | --- | --- | --- |
| 4 | `verify` | `verify(sky, perceptual.color, "green")` → cleanup winner is "blue" ≠ "green" → **fail** | `{verdict:"fail"}` | escalate (on_fail) |
| 5 | `escalate` | trigger `low-margin`, `to: frontier`; frontier returns `FrontierDeclined(402)` → **fall back local** | `{lane:"local", text:"blue"}` | verify |
| 6 | `verify` | re-check "blue" → **pass**, margin 0.41 | `{verdict:"pass"}` | assert |
| 7 | `assert` | assert | `{text:"blue"}` | end |

Note the gate's verdict is a *deterministic* function of the store — it caught
the fluent falsehood regardless of which model produced it. And the frontier
decline routed to a fallback, never a crash.

### The refuse path (empty-cell invariant)

If `recall` returns no rows and no lane can ground an answer, the micro rotor's
whole belief order is vetoed. `verify` yields `fail`, `escalate` exhausts the
ladder, and `assert` hits `empty_cell: refuse` → `text = "I don't know."`.
Refusal is a *terminal outcome*, not an error.

### Replaying this run

Replaying with the recorded event history:

- ticks 0–2 (`write`, `recall`, `compose`): recorded outputs returned as-is.
- tick 3 (`reason`): the model is **not** re-invoked; the recorded `{text:"blue"}`
  is returned. The idempotency key (`sha256(0.1.0, reason, canonical(in),
  space_id)`) guarantees the effect isn't re-fired.
- tick 4 (`verify`): the deterministic gate recomputes to the same verdict.

The sequence of transitions is reproduced exactly — that is what "deterministic
loop" delivers, without ever claiming the model would emit the same tokens twice.

### A warm result-cache hit, checkpointed

Now suppose `recall` declared `cache: { key: auto, ttl: 1h, scope: tenant }`. On a
**second, unrelated run** in the same tenant that also resolves `entity = "sky"`,
tick 1 is a **cache hit**: `cache_get` returns the earlier
`{rows:[{color:"blue"}]}` under the shared content-address, the engine writes that
value into the *new* run's `StepRecord` (with a `cache: hit` frame) instead of
touching SQL, and control proceeds exactly as if `recall` had executed. The saving
is real (no query) and the record is indistinguishable from a fresh one downstream.

When **that** second run later replays, tick 1 is now a plain recorded output — the
top branch of the loop fires and the cache is **not** consulted again. So even if
the entry has since expired or been evicted, the replay is bit-for-bit the same.
The hit saved the query; it never touched a transition. Prompt caching on `reason`
(marking the system/facts prefix cacheable) would, independently, cut that step's
input-token bill — visible only in the recorded `usage`, never in its `{text:...}`
output.
