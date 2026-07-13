# RotorSpec Concepts

A developer's guide to the ideas behind RotorSpec — the loop primitives (rotor,
step, gate, escalation, gateway, attention), the **trust layer** that makes a rotor
governable (identity, security, governance, assurance), the **runtime** that pools
and routes fungible instances at scale (pooling & affinity), and the glyphh HDC
substrate they run on (roles, segments, layers, cortex, the universal 7×33 schema,
and NL→sim grounding).

Read [SPEC.md](../SPEC.md) for the normative definitions. This document builds
intuition.

---

## Part 1 — The loop primitives

### Rotor

An AI agent is a loop. Normally that loop is implicit — it lives in a model's
head, re-derived from the prompt every turn, unauditable and unrepeatable. A
**rotor** makes the loop an artifact: a versioned document that declares the
steps an agent takes for a *class* of task.

The central design commitment, borrowed from durable-execution systems, is a
split:

- The **control plane** — *which step runs next, given the recorded state* — is
  deterministic and replayable.
- The **data plane** — *what a model or tool actually returns* — is stochastic,
  but its result is checkpointed.

So a run is faithfully replayable even though the model is not bit-reproducible:
you replay the *decisions* and return the *recorded results*.

Most tasks use the **base rotor**: `ask → plan → execute → test`. Teams add
**domain rotors** for web dev, slide decks, marketing campaigns, or corp-data
pipelines. All share one document shape.

### Two loop granularities

RotorSpec deliberately models **two nested loops**, and you should keep them
distinct:

- **The macro rotor** runs **once per prompt**: `WRITE → RECALL → REASON →
  (ESCALATE)`. Ingest the turn, retrieve grounding, compose a bounded system
  block, answer or escalate. This is the visual React-Flow loop.
- **The micro rotor** runs **once per decoded word**: `propose → dispose →
  backtrack → assert|refuse`. Propose a candidate filler (by model belief order
  or a zero-model geometric-fluency ranker), *dispose* it against HDC grounding,
  *backtrack* to the next belief on veto, and *assert* the first grounded filler
  — or *refuse* when the whole belief order is vetoed (the **empty-cell
  invariant**).

A macro step (say, a `gate` or a grounded `model` step) can internally run the
micro loop. Word-level, not char-level — the granularity is a decoded word.

### Step

A **step** is one node in the rotor's transition graph. Every step has:

- a **typed I/O signature** (`in` / `out`) so it is contract-checkable and
  composable — the DSPy "signature" discipline;
- a `type` from a **small closed catalog** (§7 of the spec);
- declarative **error handling** (`retry` / `catch`);
- an **idempotency key** if it is effectful.

The catalog is intentionally minimal. Anthropic's five agent patterns — prompt
chaining, routing, parallelization, orchestrator-worker, evaluator-optimizer —
are *compositions* of these primitives, not new step types. Keeping the set
small and canonical (one serialization per construct) is what makes rotors
portable; BPMN's sprawling vocabulary is the cautionary tale.

### Frames

A step doesn't return one value — it emits a **stream of typed frames** as it
runs: `propose`, `parse`, `dispose`, `backtrack`, `gate`, `assert`, `refuse`, a
`cache` frame recording a cache hit/miss/write, and the transport frames `delta` /
`tool` / `done`. This mirrors the reference
engine's generator (`RotorEngine.run()` yields string frames) and the attach
channel that streams them. Frames are what a visual builder animates, and they
are what one step feeds into the channel the next step reads — that's how rotors
compose.

### Gate

A **gate** is the mechanism that makes a stochastic step safe. It wraps a step's
input or output in a **deterministic accept / reject / escalate** contract. The
same primitive covers five things that look different but are one mechanism:

- **schema validation** (structured-output check),
- **assertion** (DSPy-style constraint),
- **HDC ground check** (the dispose step — see Part 2),
- **evaluator threshold** (a score gate),
- **human approval** (an interrupt-and-pause).

The insight worth internalizing: *validation failure and an approval pause are
the same mechanism.* Both interrupt, checkpoint state, and wait — one for a
retry/catcher, one for a human. A gate's verdict is a **deterministic function of
recorded state**, even when the thing it's judging came from a stochastic model.

### Escalation

When a gate rejects or a step can't ground an answer, the rotor **escalates** up
a ladder: **local → frontier → human**.

- **Local** is the free, default rung (on-device model).
- **Frontier** is the metered middle rung (a governed gateway; credits burn only
  here). A `FrontierDeclined` (HTTP 402/403/429) is a *governance* decline —
  credits, role, or rate — **not** a transport error, and it falls back to local
  so the user keeps working.
- **Human** is the top rung: surface the refusal ("I don't know.") or route to a
  person.

Escalation is the same mechanism as a pause: a trigger interrupts, checkpoints,
and routes.

### Gateway

Every time a rotor talks to the outside world — a model endpoint, an MCP tool, a
client UI — it goes through the **gateway**: the transport and governance layer
that normalizes what goes in and what comes out. Think of it as the rotor's
switchboard. It does four jobs:

- **Adapters** — every input in and every output out is normalized to the rotor's
  internal step I/O, so a `model` step's `in`/`out` looks the same whether it hit
  a local `llama-server` or a frontier Anthropic endpoint.
- **Protocol / format translation** — it translates across **MCP ↔ provider HTTP
  wire (Anthropic `/v1/messages`, OpenAI chat-completions) ↔ internal step I/O**.
  One turn can round-trip `MCP → API → format → MCP → API`; you write one typed
  signature and never a wire format.
- **Rate limiting & throttling** — per provider, per rotor, per step, per tenant,
  with queueing and backoff (on the logical clock, so replay stays stable). A
  sustained breach becomes a `budget-exceeded` trigger, not a crash.
- **Metering & FinOps** — it is the cost meter: usage/credits per call, spend
  caps, and attribution. The rule that pays the bills: **cloud LLM calls route
  through the glyphh server and are metered; local calls go straight to local
  models and are free.** Local inference never proxies the gateway.
- **Prompt caching** — it lowers a step's cache breakpoints to the provider's
  prefix cache (Anthropic ephemeral / OpenAI automatic) and meters the resulting
  **write / hit / miss** tokens distinctly. See the *Caching* note above.

Intercepting your Claude UI is *one* thing the gateway can do — but the gateway
*is* the mechanics of the transport layer, not that one trick. In glyphh-rotor the
reference is the `models/` Slicer (`/v1/messages`, local vs frontier lanes) plus
adaL's `/v1/messages` proxy. See [SPEC.md §8](../SPEC.md) and
[glyphh-integration.md §4](glyphh-integration.md).

### Attention

If escalation is "which model," **attention** is "how long, and on what." It is
the **dual of escalation**, and it is exactly two things:

1. **A budget** — how long the rotor may keep rotating around the *stator* (the
   fixed step graph + router). Expressed as wall-clock time, number of
   revolutions/iterations, tokens, and/or cost. When the budget runs out the rotor
   must **stop**, **escalate**, or emit **best-effort** — your choice.
2. **Signal weights** — how strongly each signal in the rotor document is
   weighted. Steps, context references, and signals carry weights that say what to
   focus on and how hard. A higher-weighted signal gets more focus, holds the
   rotor's attention longer, and biases the router toward it when deciding where
   context flows next.

The subtlety worth keeping straight: weights are **soft biases on routing, not new
edges.** They reorder the choices the graph already declares; they never invent a
transition. That is what keeps attention compatible with determinism — a weight is
a recorded part of the definition, so the next-step decision stays a pure function
of recorded state. And a wall-clock budget is *measured* but never a live control
predicate: the exhaustion event is checkpointed at the tick it fires, so replay
makes the same stop/escalate call. See [SPEC.md §10](../SPEC.md).

### Checkpoint & replay

After each step, the engine appends a **`StepRecord`** to an append-only event
history: the step's input hash, idempotency key, `space_id`, status, output, and
frames. **Replay** re-runs the control plane against that history; for any step
that already has a record, its output is *returned as-is* — the model is never
re-invoked. This is what "deterministic loop" actually means in practice: the
transitions are recomputed; the stochastic results are recorded once.

### Caching

There are **two** caches in RotorSpec, and they solve different problems. Keep
them apart:

- **Prompt caching** (a *cost* lever, `config.cache` on a `prompt`/`model` step).
  An agent loop re-sends a huge, near-identical prefix every turn — the system
  prompt, the tool definitions, a long stable context. A **breakpoint** marks that
  prefix as cacheable; the **gateway** translates it to the provider's prefix cache
  (Anthropic `cache_control: {type: ephemeral}`, OpenAI automatic prefix caching)
  so the provider re-reads the suffix only. It is the single biggest cost/latency
  win in an agent loop. Crucially it **does not change the model's output** — only
  the bill and the latency — so it is *determinism-neutral*. Its only trace is in
  `usage`, where the gateway breaks tokens into **write / hit / miss** because each
  is priced differently ([SPEC.md §8.6](../SPEC.md)).
- **Result caching** (a *reuse* lever, `cache` on a step's envelope). Memoize a
  step's whole *output* so a **different run** can reuse it — bounded by a `ttl`
  and a `scope` (`run | rotor | tenant | global`).

**Result caching vs. idempotency.** These look alike but aren't:

- *Idempotency* (§5.6) dedupes an effect **within one run** — a retry or replay of
  *this* run returns the recorded result and never re-fires.
- *Result caching* (§5.7) is the **cross-run** generalization: two different runs
  reuse the same output when their content-addressed inputs match. `key: auto`
  **reuses the exact idempotency key** — content-addressed over
  `(definitionVersion, step_id, canonical(in), space_id)` — so caching isn't a new
  addressing scheme, just the same address with a longer `ttl` and a wider `scope`.
  `scope: run` is essentially idempotency; `rotor`/`tenant`/`global` widen the
  reach (and require `space_id` in the key so a hit can never bind a foreign HDC
  space).

Pure, deterministic steps (`retrieve.*`, `transform`, `hdc.map`, `tool` with
`idempotency: auto`) may cache by **default**. Caching a **`model`** *result* is
**opt-in** — it freezes one stochastic sample and serves it to later runs — which
is a different thing from prompt-caching a model's prefix (always available).
`cache: none` turns it off.

**Why caching stays honest.** A result-cache **hit is recorded in the
`StepRecord`** as the step's output at first execution — exactly like a fresh
result. Replay of that run returns the record and **never re-checks the cache**, so
a later `ttl` expiry or eviction can't change a recorded run. That's the same
discipline as checkpoint/replay: the loop is deterministic because the *decisions*
are recomputed while the *results* — cached or fresh — are recorded once.

### The trust layer: identity, security, governance, assurance

Four dimensions make a rotor safe to run inside a company, and they share **one
principle** — internalize it before the details:

> A rotor **declares** what it needs; the platform (for a principal) **grants** the
> actual policy; the runtime **enforces** it at execution — deny-by-default. **A
> rotor can never grant itself anything.**

The declarations are portable and live in the document (`spec.identity`,
`spec.policy`, `spec.access`, `spec.assurance`). The grants are external and
org-owned — they never ship in the document, which is exactly what keeps a rotor
portable (G5). Enforcement happens in the run loop and at the gateway.

#### Identity & principals

Every run has **two** identities:

- the **principal** — the authenticated caller (a user or a service account),
  established by the platform's device-auth session / JWT, carrying the scopes the
  run may use;
- the **agent identity** — the rotor instance itself (`name@version` + `run_id`),
  the unit of attribution and inter-rotor calls.

Both are written into every `StepRecord`, so the event history is a complete audit
trail: *who authorized this, and what acted.* When a rotor calls a sub-rotor,
identity **attenuates** — the callee runs under the same principal but with the
**intersection** of the caller's grants and its own declared needs. A callee can
never out-scope its caller. See [SPEC.md §11](../SPEC.md).

#### Security

The security model rests on one boundary: **content from a connection or the stator
is DATA, never INSTRUCTIONS.** Retrieved memory, a tool result, a CRM record — none
of it may steer control flow. RotorSpec mostly enforces this *by construction*: a
`branch` runs pure predicates over typed values (it can't "execute" text it read),
`retrieve.sql` forbids model-written SQL, and `plan` constrains the model to schema
enums. What's left, an **input firewall** scans (the glyphh firewall-scanner) —
flagging injection / jailbreak attempts as an anomaly before the model ever sees
them.

Two more rules. **Secrets isolation** — connection credentials are gateway-held and
write-only; they never enter the model context, a frame, or a rotor output (the
rotor names a connection, the gateway attaches the secret). **Effect sandboxing** —
every effect runs least-privilege, deny-by-default: only the tools / models /
connections / data it was granted. Transport security just *is* the gateway: it's
the single egress, so nothing bypasses metering, redaction, or the scanners. See
[SPEC.md §12](../SPEC.md).

#### Governance

Governance is the org-owned control plane — the **grant** side of the principle. It
has four parts:

1. **Control policy** — allow / deny-lists over step types, tools, models, and
   connections, plus **required human-approval gates** (which just compose with the
   `gate` / `wait` primitives you already have). glyphh's capabilities / guardrails
   model is the reference.
2. **Budget governance across all rotors** — a hierarchy: a run's attention budget
   ⊂ a per-rotor budget ⊂ a **tenant / org cap that spans every rotor**. When the
   org cap is spent, *every* rotor throttles / stops / escalates — aggregate FinOps,
   above any single run's budget.
3. **Routing governance** — which models / providers / rotors a principal may route
   to (e.g. *PII → local-only*, *approved-providers-only*). The **gateway enforces;
   the router only obeys.**
4. **Data-access governance** — deny-by-default grants scoped to connector /
   operation / **field** (field- / row-level is the floor) and to which memory
   **spaces / entities / roles** a rotor may read / write. The crucial move:
   **ungranted fields are redacted *before* they enter the rotor's context** — a
   marketing rotor doesn't just ignore the finance space, it never receives it. See
   [SPEC.md §13](../SPEC.md).

#### Assurance & observability

Assurance makes behavior measurable. Every step emits **effectiveness metrics** —
gate-pass rate, escalation rate, cost-per-success, evaluator quality — as telemetry
that feeds both the evaluator-optimizer `loop` and your dashboards. And **anomaly
detection is a first-class signal, not a log line**: an *input* anomaly (the
firewall catching an injection) or an *output* anomaly (a PII / policy leak, drift,
or — the important one — an **ungrounded assertion**) is a gate verdict that can
**escalate**. That last case is the HDC gate doing its job: *the model asserted what
memory did not return* becomes a refuse, deterministically, regardless of which
model said it. See [SPEC.md §14](../SPEC.md).

### Pooling & affinity

Everything above is about what a rotor *is* and how *one run* executes. The last
piece is **operational**: how a runtime is deployed and scaled so runs are served
fast at scale. It rests on one property — **a rotor runtime is stateless.** All
run-critical state (the Context, the event history, the HDC store, the caches)
lives in the **stator**, not in the process; the gateway is its only transport
boundary. So instances are **fungible** — any instance can serve any run — which is
exactly what makes a runtime a **Kubernetes Deployment of interchangeable pods**
you scale horizontally. A pod that dies is replaced and the run continues on its
successor, because the state was never in the pod.

Fungible instances are managed as a **pool**, and each is **hot**, **warm**, or
**cold**:

- **Hot** — fully warmed, serving immediately: the local model is loaded (the big
  one — GGUF weights in RAM/VRAM, `llama-server` ready), stator connections open,
  caches primed. An idle hot instance costs money.
- **Warm** — loaded but suspended (a Fly Machine `resume`, a parked min-replica);
  fast to bring back, not instant.
- **Cold** — not instantiated; the first request eats the full cold-start.

You **pre-warm** against expected demand — *"we'll get a burst at 09:00, have hot
instances ready"* — by declaring a `pool` block (`minHot` / `maxHot` /
`targetConcurrency` / a warm `schedule` or `predicted`). Same **declare → grant**
discipline as the trust layer: the rotor declares its warmth *intent*; the platform
**provisions** the real pool against real demand — and **the warm pool is bounded
by the org budget** ([SPEC.md §13.2](../SPEC.md)). Idle hot instances are a FinOps
cost like any metered call, so a rotor can no more self-grant warm capacity than it
can self-grant a scope; when the org cap is spent, pre-warming is the first thing
shed.

The router then does **affinity**: prefer a warm instance whose stator/cache
*already holds* the matching state (`keys`: `tenant`, `conversation`, `entity`), so
warm state and cache hits are reused instead of re-primed. Default `mode: prefer`
is a soft bias that **gracefully falls back** to any instance (or a cold-start);
`mode: require` is a hard pin — powerful, but it can **starve throughput** when the
affine instance is saturated, so reserve it for state that genuinely can't move. A
`sub-rotor` call routes into its own pool and **propagates the affinity keys** —
but never widens authority: identity still attenuates across the handoff, so the
affine instance an effect lands on carries no grant of its own.

And it is all **determinism-neutral** — the same honesty as caching. *Which*
instance served a run and *whether* it was hot or cold is **operational metadata**
for observability, never a control input; a run served cold and the same run served
hot record the identical event history. Pooling and affinity make runs *cheaper and
faster*, never *different* — so they are **optional at every conformance level** and
a runtime that ignores them entirely is still fully conformant. See
[SPEC.md §17](../SPEC.md).

---

## Part 2 — The glyphh HDC substrate

RotorSpec's grounding gates run on **hyperdimensional computing (HDC)** — the
glyphh encoder. This is the "NL → sim" mapping the founder describes: natural
language becomes a point in a high-dimensional vector space where you can *check*
whether a claim is actually stored, rather than trusting a model's fluency.

### The grounding law (the most important idea)

Groundedness is **not** whole-sentence cosine similarity. It is **entity-keyed
associative retrieval**. A proposal is admitted only if *the entity's own memory
record returns it.*

Concretely, for a fact like "the sky's color is blue":

- **Write:** `record[sky] = bundle over bind(role_vec("perceptual.color"),
  filler_vec("blue"))`. The entity's record is a superposition of its
  role→filler bindings.
- **Read (probe):** `probe(sky, perceptual.color) = cleanup(unbind(record,
  role_vec("perceptual.color")))` — unbind the role, clean up to the nearest
  known filler.
- **Verify:** `verify(sky, perceptual.color, "blue")` returns
  `(grounded, membership, margin, top)`, where `grounded` is true **iff** the
  cleanup winner *is* "blue" **and** the margin over the second candidate exceeds
  a threshold.

A `retrieve.kb` or `gate` step therefore **requires an entity and a role** — never
just "text similarity." A rotor that tries to ground on whole-sentence cosine is
using the wrong primitive; the codebase deliberately ports away from it.

### Roles, segments, layers, cortex

Four terms you'll see constantly:

- **Role** — a slot key of the form `layer.role` (e.g. `perceptual.color`,
  `relational.object`). Roles are what the encoder binds fillers against. There
  are **33** of them.
- **Layer** — one of **7** groupings of roles. Layers are *also* addressable
  subtree keys in the hierarchical memory (you can extract "everything in the
  `perceptual` layer for entity X").
- **Cortex** — the bipolar hypervector that *is* an entity or fact — the result
  of bundling all its `bind(role, filler)` terms. "The cortex for `sky`."
- **Segment** — **not a distinct object.** Segmentation just means *which slot a
  surface token belongs to* — the per-word tagging of a token as a content slot
  (`layer.role`) or a function word (`O`). Don't model it as a store; it's a
  token-level label. (Also: the layer subtree in hierarchical memory is the
  nearest "grouping" concept.)

### The universal 7×33 schema

The encoder binds against a **fixed lattice** of 7 layers × 33 roles —
`UNIVERSAL_SCHEMA`. It is the closed vocabulary a slot picker chooses from:

| Layer | Roles |
| --- | --- |
| **entity** | name, kind, subkind |
| **perceptual** | color, size, shape, texture, sound, smell, taste, temperature |
| **spatial** | location, origin, direction |
| **temporal** | time, duration, age, era, frequency |
| **relational** | subject, predicate, object, possessor, agent, patient, instrument |
| **quantitative** | count, magnitude, unit, ratio |
| **epistemic** | source, certainty, modality |

Two validation moves keep the lattice clean:

- `_sanitize_universal(facts)` **refuses** off-schema layers/roles and
  empty/none/null/n-a values. This is *structural ∅* — a deliberately empty cell,
  not a confabulation.
- `universal_role_fillers(facts)` flattens sanitized `{layer:{role:value}}` into
  `[(layer.role, value)]`. **This flattening is the bridge from an NL fact to a
  hypervector.**

### NL → sim, both directions

The whole "NL → sim" grounding is precisely two pipelines:

**Write (NL → cortex):**
```
NL text → enricher → {layer:{role:value}} → _sanitize_universal
        → universal_role_fillers → Encoder.encode → cortex
```

**Read (NL question → grounded filler):**
```
NL question → (entity, role) → unbind(cortex, role_vec) → cleanup → grounded filler
```

The HDC algebra underneath (`hdc.py`): `bind` = elementwise bipolar product
(self-inverse, so `unbind == bind`); `bundle` = majority-sign superposition;
`raw_cosine` ∈ [−1, 1]. Atoms are **deterministic per `(name, seed)`** via
`sha256 → seeded RNG → ±1`, so two writes of the same fact land in the same
place. That determinism is exactly why a ground gate's verdict is reproducible
even when the *proposal* came from a stochastic model.

### The space invariant

A vector only means something within its space. `space_id =
sha256(vector_dim, encoder_seed, roles_config)`. `assert_space()` refuses to bind
across mismatched spaces — the result "would return noise." Every retrieval,
gate, and encode step carries and validates a `space_id`. A rotor that omits
space identity is unexecutable.

### The hard gate vs the soft gate

Two enforcement strengths, same store:

- **Hard gate (`GroundedConstraint`).** A logit-time prefix automaton masks the
  vocabulary to *only* the token continuations of the store's grounded fillers
  for `(entity, role)` — or a refusal token. The model *literally cannot emit* a
  non-grounded token. "The guarantee lives in the wrapper, not the weights." This
  needs logit access (local models, HF/MLX/vLLM/llama.cpp GBNF grammar).
- **Soft gate (verify-then-refuse).** For API models with no logit access:
  generate freely, parse the asserted `(entity, role, filler)`, `verify()` each,
  and refuse/repair on a miss. Weaker, but works over any model.

A `gate` step picks `enforcement: hard | soft`.

### Retrieval: four deterministic paths

Grounding (HDC probe/verify) is separate from *retrieval*. RotorSpec exposes four
retrieval flavors, each deterministic given the store:

1. **`retrieve.sql`** — a **closed op** over an indexed `fact_slots` table:
   `lookup / prev / count / count_not / top / who / compare / refuse`, with
   `{layer.role: value}` conditions intersected by fixed SQL templates. **No
   model-generated SQL** — runaway queries are excluded by construction. This is
   also where **exact aggregates** come from; you never decode a number from a
   vector (vectors are lossy at arithmetic).
2. **`retrieve.kb`** — entity-keyed HDC probe/verify **and** the knowledge graph
   (`node(entity)` = union of an entity's slot fills; `neighbors(entity)` =
   entities sharing a `(layer, role, value)` edge).
3. **`retrieve.vector`** — semantic recall: embed the query (nomic) and
   brute-force unit-dot rank stored turns/events (thresholds 0.35 semantic /
   0.05 lexical).
4. **Multi-hop / analogy** — over the hierarchical tree `cortex → layer → role →
   filler`: bounded `chain(start, [role1, role2, …])` with a cleanup (SNR reset)
   at each hop, or `analogy(a, filler, b, layer)` that *discovers* the relating
   role. Expressed as a `retrieve.kb` step with a role-**path** config.

### Model authority vs language

The pattern that ties it together: **let the model do language, keep authority
deterministic.** Three separable steps:

1. **Route** (`SchemaGuard.classify`) — a nearest-prototype semantic router with
   an explicit `OUT_OF_SCHEMA` negative class; route by *margin* (≥ 0.05), else
   refuse.
2. **Plan** — the model emits a **typed `Plan(op, field, tier, region, k)`** with
   slots constrained to schema enums (out-of-schema → refuse).
3. **Execute** — run the plan **deterministically over the exact store** (exact
   sum/avg/count; cohort membership via HDC cosine to a prototype).

The model chooses *what*; the lattice and the store guarantee *how*. Numbers come
from the store, never from a vector.

---

## Where to go next

- [execution-model.md](execution-model.md) — the deterministic run semantics and
  a worked trace of the base rotor.
- [glyphh-integration.md](glyphh-integration.md) — how a rotor step actually
  invokes the encoder and the retrieval paths, and how glyphh-rotor executes a
  Rotor Document.
