# RotorSpec Runtime Architecture

**How to build a conforming RotorSpec runtime — and why the open reference runtime
and the glyphh product are the same contract, not the same code.**

Spec version: **0.1** · Reference runtime: **Node/TypeScript, Apache-2.0** ·
Companion product: **glyphh-rotor (Python, closed)**

This document is the design for the runtime — the program that loads, validates,
and executes a Rotor Document. [SPEC.md](../SPEC.md) defines *what* a conforming
runtime must do; [concepts.md](concepts.md) defines the primitives;
[execution-model.md](execution-model.md) defines the loop; this document defines
*how to build a runtime that does it*, and how implementations legitimately differ.

---

## 1. Overview

### 1.1 What a rotor runtime is

A rotor runtime is a **stateless evaluator of a deterministic, governed run loop
over a versioned Rotor Document.** It is not a framework you write agents in; it is
an interpreter for a declarative artifact. You hand it a `.rotor` document and a
principal; it authenticates, walks the step graph, quarantines every
nondeterministic call inside a checkpointed `execute()`, and appends an append-only
event history that makes the run replayable, auditable, and resumable on any other
instance.

The runtime splits two planes (SPEC.md §6):

- a **CONTROL plane** — *which step runs next* — a pure function of recorded state,
  reproducible bit-for-bit forever; and
- a **DATA plane** — *what a model or tool returned* — stochastic, but checkpointed
  the instant it happens, so replay returns the recorded result and never
  re-invokes the model.

Everything else in this document falls out of holding that line.

### 1.2 The open-reference-vs-glyphh-product relationship

RotorSpec is **open-core by CONTRACT, not by a shared codebase.** There are two
runtimes, on purpose — independent implementations are how a standard proves it is
real:

| | **Reference runtime** (this repo) | **glyphh product** (glyphh-rotor) |
| --- | --- | --- |
| Language | Node / TypeScript | Python |
| License | Apache-2.0, open | Proprietary, closed |
| Role | The conformance demo — readable, installable, honest | The production runtime — HDC grounding, full stator, hosting, governance |
| Grounding | Abstracted: exact-match / soft verify-then-refuse. **Does not practice the patent.** | The patented HDC method behind an opaque `grounding` plugin |
| Memory | SQLite, zero-dep, bare-box | Postgres + pgvector, cascade consolidation, scaled 7×33 lattice |
| Models | Local OpenAI/Anthropic-compatible lane, free by construction | Governed frontier metering lane + local |
| Governance | Local unenforced stubs, deny-by-default defaults | Org roles/grants, credits ledger, Ed25519 license |
| Scaling | Single instance, cold-start | K8s pool, hot/warm/cold, affinity ring |

The two runtimes share **no code**. They share the **spec** — the JSON Schema, the
step semantics, the StepRecord shape, and the golden-transcript conformance suite.
A capability that legitimately differs between implementations (grounding, memory,
models, gateway, governance, pooling) sits behind a **stable plugin interface**; the
reference runtime ships a basic implementation of each, and glyphh drops in a
premium one behind the same interface.

### 1.3 The "build once, run anywhere" promise

A developer authors a rotor **once**. The same document runs:

- **self-hosted** on the open reference runtime on a bare box — SQLite stator,
  local model, exact-match grounding, no metering; or
- **on glyphh** — Postgres stator, HDC grounding with hard logit gate, governed
  frontier lane, a warm pod pool.

Same document, richer runtime. This is not aspirational — it is the
**graceful-degradation path the code already takes.** Every premium subsystem has a
working open fallback selected at runtime; binding a different plugin swaps the
implementation without touching the document. The contract that makes this true is
**capability negotiation** (§3.8): the rotor *declares* the capabilities it needs,
the runtime *advertises* what it has, and a missing premium capability produces
either a documented graceful degradation or a clean refuse — **never a silently
wrong answer.**

---

## 2. Architecture

### 2.1 The core engine

The engine's job is `load → validate → execute`. Three phases, one long-lived
process, many runs.

```
                        ┌───────────────────────────────────────────────┐
   .rotor document ───▶ │  LOAD           parse (YAML/JSON) → AST         │
                        │                 resolve $refs, defaults         │
                        ├───────────────────────────────────────────────┤
                        │  VALIDATE       JSON Schema (L1)                │
                        │                 static graph checks:            │
                        │                 - closed step catalog           │
                        │                 - every path reaches a terminal │
                        │                 - typed state schema present    │
                        │                 - reducers for fan-in keys      │
                        │                 - space_id declared             │
                        │                 - loop caps present             │
                        ├───────────────────────────────────────────────┤
                        │  PIN            definitionVersion → run_id       │
                        └───────────────────────────────────────────────┘
                                            │
                                            ▼
   ┌──────────────────────────────────────────────────────────────────────────┐
   │                              RUN LOOP  (§2.2)                             │
   │                                                                          │
   │   authenticate(principal) ─┐                                             │
   │   resolve_grants ──────────┴──▶ [checkpoint grants once]                 │
   │                                                                          │
   │   cursor := entry                                                        │
   │   while cursor not terminal:                                             │
   │     step := def.steps[cursor]                                            │
   │     require_policy(step, grants)          ── trust point 2               │
   │     input := resolve(step.in, Context)                                   │
   │     input := redact(input, grants)        ── trust point 3               │
   │     if history has record(step.id, attempt):                            │
   │         output := record.output           ── REPLAY (no execute)         │
   │     else:                                                                │
   │         output := cache_get(step, input)  ── §5.7 result cache           │
   │         if MISS:                                                         │
   │             output := execute(step, input, grants)  ◀── DATA plane      │
   │     append StepRecord                                                    │
   │     merge(output → Context, reducers)                                    │
   │     cursor := select_next(step, output, Context)  ◀── CONTROL plane      │
   │     logical_tick += 1                                                    │
   │   project spec.outputs                                                   │
   └──────────────────────────────────────────────────────────────────────────┘
                    │                     │                    │
                    ▼                     ▼                    ▼
        ┌──────────────────┐  ┌────────────────────┐  ┌──────────────────┐
        │  PLUGIN REGISTRY │  │      GATEWAY       │  │  EVENT HISTORY   │
        │  (§3)            │  │  (§3.5, SPEC §8)   │  │  StepRecord store│
        │                  │  │  every effectful   │  │  (stator, §3.2)  │
        │  grounding       │  │  I/O crosses here: │  │                  │
        │  memory/stator   │  │  adapters, xlate,  │  │  single source   │
        │  models          │  │  rate limit,       │  │  of truth for    │
        │  connections     │  │  metering, cache   │  │  replay          │
        │  gateway adapters │  │  lowering          │  │                  │
        │  governance      │  │  INSIDE checkpoint │  │                  │
        │  pool/affinity   │  │  boundary          │  │                  │
        └──────────────────┘  └────────────────────┘  └──────────────────┘
```

The five **trust-layer enforcement points** (SPEC.md §11–§14) are the annotations
on the loop above; they are not extra machinery. The **firewall** (input scan) and
**anomaly/ground** (output scan) are `gate` steps *in the graph*, not hidden
passes — enforcement point 4 and 5 are ordinary steps the author places.

### 2.2 The run loop

The loop is the whole runtime; everything else is a plugin it calls. Its contract
(SPEC.md §5, execution-model.md §1):

1. **Once per run:** authenticate the principal, resolve grants, checkpoint the
   grant set into history. Two identities are fixed for the run — `principal`
   (authenticated caller + scopes) and `agent_identity` (`name@version` + `run_id`)
   — and both land in every StepRecord.
2. **Per step:** `require_policy → resolve → redact → (replay | cache_get |
   execute) → append StepRecord → merge → select_next → tick++`.
3. **At the end:** project `spec.outputs` from Context.

The only nondeterministic line is `execute()`. Everything above and below it is a
pure function of recorded state.

### 2.3 Step dispatch by type

Dispatch is a **closed catalog of 20 step types** (SPEC.md §7). The engine holds a
`Map<StepType, StepHandler>`; each type maps to exactly one subsystem verb. This
table *is* the boundary between the engine and the plugins:

| Step type | Plane | Subsystem verb | Determinism |
| --- | --- | --- | --- |
| `prompt` | control-ish | compose (pure) | deterministic |
| `model` | **data** | models.execute — **quarantined** | stochastic, checkpointed |
| `hdc.map` | data-ish | grounding.encode (NL→cortex) | deterministic given store+space |
| `write` | effect | memory.write — idempotent | recorded |
| `retrieve.sql` | control | memory.execute_op — closed ops, **no model SQL** | deterministic given store |
| `retrieve.kb` | control | grounding.probe/verify + graph | deterministic given store |
| `retrieve.vector` | data | memory.semantic_recall (embed+rank) | embed stochastic, rank deterministic |
| `gate` | control | grounding.verify / firewall / anomaly (7 modes) | deterministic given store |
| `assert` | control | grounded terminal / refuse | deterministic |
| `plan` | data | models.classify → typed Plan → deterministic execute | split |
| `branch` | **control** | pure-predicate router | deterministic |
| `loop` | control | bounded evaluator-optimizer (caps mandatory) | deterministic |
| `parallel` | control | fan-out + reducer | deterministic merge |
| `wait`/`interrupt` | control | pause-checkpoint-resume | deterministic |
| `escalate` | effect | models.escalate ladder | recorded |
| `tool` | effect | connections.dispatch (mcp/app) | recorded |
| `transform` | control | pure fn | deterministic |
| `cascade` | effect | memory.consolidate | recorded, out-of-band |
| `sub-rotor` | control | nested run — frames join parent | deterministic control |
| `fail` | control | typed terminal | deterministic |

Anthropic's five agent patterns (prompt chaining, routing, parallelization,
orchestrator-worker, evaluator-optimizer) are **compositions** of these, never new
types. A handler receives `(step, input, grants, ctx)` and returns a `StepResult`
(output + frame stream); the engine records it and merges it. The handler never
picks the next step — `select_next` does.

### 2.4 select_next — the control plane

`select_next` (SPEC.md §5.1, execution-model.md §4) is a **pure function of
recorded state**. Resolution order:

1. `branch` → first matching ordered pure predicate over Context, else `default`;
2. `gate` verdict → `on_pass` / `on_fail` / `on_escalate`;
3. `loop` → re-enter body while the gate rejects **and** iterations < max **and**
   budget remains, else `on_exhausted`;
4. else `step.next`.

Invariants the engine enforces:
- Iteration is in **recorded / sorted-key order** — never map/set insertion order.
- Attention **weights bias ordering among already-eligible successors; they never
  add edges.**
- Budget exhaustion short-circuits to `stop | escalate | best-effort` and is
  **checkpointed at the tick it fires** (replay reproduces the same decision).
- Reserved terminals: `end`, `__fail__`, a refusing `assert`. Static validation
  guarantees every path reaches one.

Because `select_next` reads only typed Context values through pure predicates,
retrieved memory and tool results are **values, never steps to run** — the
instruction-source boundary (SPEC.md §12.2) is enforced by construction.

### 2.5 The event-history / StepRecord store

After every step the engine appends an append-only **StepRecord** (SPEC.md §5.4):

```
StepRecord {
  run_id, step_id, attempt, logical_tick,
  input_hash      = sha256(canonical(input)),
  idempotency_key = sha256(definitionVersion, step_id, canonical(in), space_id),
  space_id, principal, agent_identity,
  status          ∈ {ok, refused, failed, escalated, interrupted},
  output, frames, error
}
```

The event history in the stator is the **single source of truth.** It is what makes
instances fungible (any pod can load the pinned version and continue), runs
resumable (a `wait`/`interrupt` checkpoints and resumes on a different pod), and
runs replayable. Frames (SPEC.md §2) — `propose, parse, dispose, backtrack, gate,
assert, refuse`, a cache frame (`hit/miss/write`), and transport frames
(`delta/tool/done`) — are recorded inside the StepRecord; a visual builder animates
them, and composition works by **piping** one step's frame stream into the next.

### 2.6 Checkpoint + replay

Replay re-runs the **control plane** against history:

- if a record exists for `(step_id, attempt)` → return its output **as-is**; the
  model/tool is **not** re-invoked;
- only recordless steps execute.

This is the load-bearing property of L3. The gateway sits **inside** the checkpoint
boundary — replay returns normalized I/O, never a fresh wire read (§3.5). The result
cache is **never re-consulted on replay** (§3.2) — a recorded run's transitions can
never change because a cache entry expired.

### 2.7 The determinism boundary

The runtime **MUST NOT** let the control plane depend on (SPEC.md §6):
- wall-clock in a predicate — use the **logical clock** (`logical_tick`, §5.3);
- un-seeded / un-recorded RNG;
- unordered map/set iteration — **iterate by sorted key**;
- a live tool read not in the event history.

Honest determinism: the runtime records model tokens; it **does not promise to
reproduce them** (FP drift, batch scheduling, provider changes). Real determinism
re-enters through the **HDC gate**: `verify(entity, role, filler)` is a pure
function of `(entity, role, filler, store-state, space_id)`, independent of which
model proposed the filler. Idempotency (§5.6): every effectful step carries an
`idempotency_key`; on retry/replay a completed effect with the same key returns the
recorded result and does **not** re-fire. `idempotency:none` MUST NOT appear in an
L3 rotor.

---

## 3. The plugin model

A runtime is the run loop **plus seven pluggable capabilities.** Each capability is
a stable interface; the reference runtime ships a basic implementation; glyphh drops
in a premium one behind the same interface. This section gives, for each: the
**interface contract**, the **open basic implementation**, and **how glyphh swaps
in.**

Every capability follows the glyphh-rotor **subsystem shape** (glyphh-integration.md
§1): it is lazily constructed, answers a uniform `status() → { ready, detail }`, and
**degrades (`ready: false`) rather than raising.** That shape *is* the plugin seam.

```
interface Capability {
  readonly name: string;              // "grounding", "memory", ...
  status(): { ready: boolean; detail: string; tier: "basic" | "premium" };
}
```

### 3.1 Grounding / encoder

The NL→sim bridge and the ground verdict (SPEC.md §6.3, §15.4, concepts Part 2).

**Interface contract:**
```
interface GroundingPlugin extends Capability {
  computeSpaceId(vectorDim, encoderSeed, rolesConfig): SpaceId;
  assertSpace(spaceId): void;                       // refuse cross-space binds
  encode(roleFillers, spaceId): Cortex;             // NL fact → cortex  (hdc.map)
  probe(entity, role, spaceId): { filler, membership, margin, top };
  verify(entity, role, filler, margin, spaceId):
        { grounded, membership, margin, top };      // grounded iff winner==filler && margin>thr
  groundedFillers(entity, role): Token[];           // hard-gate mask source
}
```
`grounded` iff cleanup-winner == filler **and** margin > threshold (admit 0.10,
margin 0.05). The **space invariant** is mandatory: `space_id = sha256(vector_dim,
encoder_seed, roles_config)`; every `hdc.map`/`write`/`retrieve.*`/`gate` step
carries and validates it; a rotor omitting space identity is **unexecutable**.
Numbers/aggregates come **only** from the exact store (`retrieve.sql`), never
decoded from vectors.

**Open basic implementation:** a pure-numeric `ExactMatchGrounding` — exact lookup
over the SQLite `semantic` fact column: return the stored filler for `(entity,
role)` if present, else refuse; optional **soft** verify-then-refuse (generate,
parse the asserted triple, check it exists, repair/refuse on miss). Deterministic
and honest, just not associative. **No hard logit gate.** Boots on a bare box with
zero deps. *The reference runtime does not implement the HDC algebra — it abstracts
`hdc.map` and does not practice the patent.*

**glyphh premium swap-in:** the patented HDC grounding engine + encoder as an opaque
`grounding` plugin (RuntimeStore against the production glyphh store). Geometric
membership verify with margin, fluency-vs-truth veto+backtrack, and a **hard
logit-time `GroundedConstraint`** that masks the vocabulary to grounded
continuations or a refusal token — the model literally cannot emit an ungrounded
token. *What* it guarantees is the interface; *how* (the encoding method) stays
behind `verify()` / `groundedFillers()` and is never exposed.

### 3.2 Memory / stator

The persistent backend holding **all** run-critical state — Context, StepRecord
history, HDC store, result cache (SPEC.md §5.2, §5.7, §7 retrieve/write/cascade).

**Interface contract:**
```
interface MemoryPlugin extends Capability {
  // retrieval (deterministic-given-store)
  executeOp(op, conditions, spaceId): Rows | Count;  // closed op set, NO model SQL
       // op ∈ {lookup, prev, count, count_not, top, who, compare, refuse}
  probe / verify / node(entity) / neighbors(entity) / chain(start, roles) / analogy(...);
  semanticRecall(queryEmbedding, topK, threshold): Hit[];
  // writes
  write(facts, { key?, mode }): void;                // tell_raw | remember | absorb
  // event history — the source of truth
  appendStepRecord(rec): void;
  readEventHistory(runId): StepRecord[];
  // result cache (§5.7)
  cacheGet(step, input): Output | MISS;              // key = idempotency_key
  cachePut(step, input, output, { ttl, scope }): void;   // scope: run|rotor|tenant|global
  // consolidation
  cascade(): void;                                   // short → mid → long
}
```
Every op carries and validates `space_id` and **intersects reads with the run's
granted spaces/entities/roles, redacting ungranted fields before returning**
(enforcement point 3). Result-cache invariants: `space_id` MUST be in the key at
tenant/global scope (refuse a global/tenant entry without it); `write` / `escalate`
/ `wait` / side-effecting `sub-rotor` MUST NOT cache above run scope; a hit is
written into the StepRecord at first execution like a fresh result, and **replay
never re-consults the cache.**

**Open basic implementation:** a SQLite `MemorySubstrate` (`~/.glyphh/rotor.db`,
stdlib, WAL, zero-dep): indexed `fact_slots` closed ops, deterministic lexical/slot
recall, an in-memory brute-force unit-dot vector rank, the EntityGraph, plus the
event-history and result-cache tables. Degrades to SQLite when no Postgres rather
than crashing.

**glyphh premium swap-in:** Postgres + pgvector (`DATABASE_URL`) for the fact/vector
store and the **shared cross-pod** event history + tenant/global result cache;
`EntityGraph` + `HierMemory` multi-hop; a nomic OpenAI-compatible embedding endpoint
for `retrieve.vector`; `cascade.py` short→mid→long consolidation; connector-
governance + `org_roles` field/row/space redaction at the retrieval boundary.

### 3.3 Models / inference lanes

The quarantined data-plane executor (SPEC.md §7 model/plan/escalate, §9).

**Interface contract:**
```
interface ModelsPlugin extends Capability {
  decide(requestBody, pickedRoute): { lane: "local" | "frontier" };
  execute(requestBody, lane): { text, frames, usage };   // normalized, checkpointed
  classify(question): { class, margin };                 // plan router
  plan(class): Plan;                                     // typed, schema-enum-constrained
  executePlan(store, plan): Result;                      // deterministic over exact store
  escalate(trigger): LadderStep;    // local → frontier → human
}
```
Only **local** inference is free by construction; **frontier** is the metering
point. `FrontierDeclined(402/403/429)` is a **governance** decline (credits/role/
rate) distinct from transport error and **falls back to local, never crashes**.
Output tokens are **recorded, never promised reproducible.**

**Open basic implementation:** a local OpenAI/Anthropic-compatible endpoint
(`llama-server` / `ollama` via `ROTOR_LOCAL_MODEL_URL`) as the sole free `local`
lane; when no model is attached, a zero-model **geometric-fluency ranker** orders
proposals so the loop still grounds/refuses; escalation degrades to the local/human
rungs only. Soft-gate verification when the model has no logit access.

**glyphh premium swap-in:** the `models/` Slicer `/v1/messages` gateway routing
local(free) vs frontier(metered), governed frontier providers (Anthropic/OpenAI)
behind credits, `SchemaGuard` nearest-prototype routing with an `OUT_OF_SCHEMA`
reject class, and **hard-gate logit access** on controlled open-weight kernels for
true token-level grounding enforcement.

### 3.4 Connections / tools

Side-effecting dispatch through one registry, plus secrets isolation (SPEC.md §7
tool, §12.3).

**Interface contract:**
```
interface ConnectionsPlugin extends Capability {
  register(method, handler): void;
  listTools(): ToolSchema[];        // JSON inputSchema per tool
  dispatch(method, args): { ok, result } | { error };   // NEVER raises → frames
  invoke(method, args): Result;                          // raises
}
```
Two flavors: `tool.mcp` (in-runtime substrate tools, each with a JSON `inputSchema`
— *the model chooses what, the tool guarantees how*) and `tool.app` (client/app
methods over the **loopback** attach channel — `panels.*` / `layouts.*` / `apps.*`).
A step **names a connection**; the gateway attaches the **write-only encrypted**
credential at the transport boundary and strips it before return — the secret never
enters input, output, a frame, or a StepRecord. Deny-by-default: only granted
tools/connections/operations/fields.

**Open basic implementation:** an in-process `HandlerRegistry` with the ~30
substrate MCP tools (`think / ask / tell / remember / recall / query / keys / …`),
**loopback-only** (the cloud reach-in was removed — nothing dials into a personal
machine), lazy-importable without the MCP SDK; app-method stubs a live client can
override.

**glyphh premium swap-in:** server-side custom-MCP / connector execution (yo-server)
with write-only encrypted connection headers, a governed connector catalog,
`org_roles` field/row scoping, and a live Electron client driving real windows/apps.

### 3.5 Gateway adapters

The single transport boundary every effectful I/O crosses (SPEC.md §8). The gateway
is a **LAYER, not a step type** — declared once as `spec.gateway`, per-step
overridable — that `model`, `escalate`, `tool.mcp/app`, and `retrieve.vector`
embeddings cross; deterministic-given-store steps never touch it.

**Interface contract:**
```
interface GatewayPlugin extends Capability {
  inAdapter(wireForm): StepIO;      // MCP result / anthropic-messages / openai-chat / UI event → internal
  outAdapter(stepIO): WireForm;
  translate(from, to, payload): Payload;   // table-driven; missing entry = E_UNTRANSLATABLE
  lowerPromptCache(breakpoints, provider): WireForm;   // unhonorable → no-op, never error
  // gateway responsibilities also cover: rate limit, metering (§3.6)
}
```
Five responsibilities: (1) **I/O adapters** — normalize every wire form so a
`model` step's in/out is identical local vs frontier; (2) **MCP↔provider-HTTP↔
internal translation**, table-driven and deterministic (missing = `E_UNTRANSLATABLE`,
never a silent drop); (3) hierarchical **rate limiting** per provider/rotor/step/
tenant, queue+backoff on the logical clock, sustained breach → `budget-exceeded` /
`frontier-decline`, never a crash; (4) **metering / FinOps** (§3.6); (5) **prompt-
cache breakpoint lowering** to provider wire form. The gateway sits **inside** the
checkpoint boundary — replay returns normalized I/O, never a fresh wire read.
**Load-bearing rule:** cloud calls route through the glyphh server and are metered;
local calls go straight to local models and are **free** — local inference MUST NOT
proxy the gateway.

**Open basic implementation:** an adaL `/v1/messages` proxy as the format/protocol
translator between provider wire formats and internal I/O; **identity adapters** and
**no-op** prompt-cache lowering on a bare box; best-effort / no-op rate limiting and
metering (local is unmetered by construction). An **absent gateway is a bare-box
identity-adapter default.**

**glyphh premium swap-in:** the full glyphh gateway — the complete MCP↔API↔format
translation table, provider-native prefix-cache lowering with write/hit/miss
accounting (Anthropic `cache_control:ephemeral` on the prefix's last block, OpenAI
stable-leading-block ordering, local KV-prefix reuse), and the client-UI
interception adapter.

### 3.6 Governance / metering

The org-owned **grant + enforce** plane the runtime consults, never authored in the
document (SPEC.md §11–§14). One principle: a rotor **DECLARES** needs (portable,
in-document), the platform **GRANTS** (external, org-owned), the runtime
**ENFORCES** deny-by-default; a rotor can **never self-grant.**

**Interface contract:**
```
interface GovernancePlugin extends Capability {
  authenticate(principal): Identity;
  resolveGrants(identity, org): GrantSet;              // checkpointed once at run start
  requirePolicy(step, grants): "allow" | "deny";       // step-type/tool/model/connection + approval
  enforceRouting(principal, laneOrProvider): "allow" | "deny";   // PII→local, approved-providers
  redact(input, grants): Input;                        // ungranted fields removed before context
  recordUsage(step, tokens, cost): void;               // cache_write/cache_read broken out
  checkBudget(): "ok" | "budget-exceeded";             // attention ⊂ rotor ⊂ org
}
```
Five enforcement points, all reading the **recorded** grant set so they stay in the
deterministic control plane: (1) run start — authenticate + checkpoint grants;
(2) before a step — `require_policy`; (3) on resolve/retrieval — redact ungranted
fields before Context; (4) before execute — input firewall gate; (5) after execute —
output anomaly gate (incl. the HDC ground gate: an ungrounded assertion IS an
anomaly → deterministic refuse). Sub-rotor identity **attenuates** — the callee runs
under the caller's principal with the **intersection** of caller grants and its own
declared needs, never out-scoping the caller. Enforcement is **replay-stable**:
grants checkpointed once, so a later re-grant/revocation can't change a recorded run.

**Open basic implementation:** an in-process `control_plane` subsystem with a local
`capabilities` / `guardrails` file, per-run/rotor budget caps, and a local
credit/usage ledger (`OperationsMeter` counts to `usage.json` but **never blocks** —
"hard enforcement deferred to the Platform"); licensing falls through to free-tier
when no pinned key. Deny-by-default with sensible bare-box defaults; declarations are
parsed, grants default-open locally (single-tenant, your own box).

**glyphh premium swap-in:** the glyphh org/policy plane — device-auth + Ed25519-
signed license JWT for principals, `capabilities`/`guardrails` matched at load,
connector-governance + `org_roles` for field/row/space grants, and the credits /
metering ledger where the Slicer frontier lane is the metering point and the org cap
spans **every** rotor.

### 3.7 Pool / affinity

The determinism-neutral operational layer (SPEC.md §17). **OPTIONAL at every
conformance level** and **forbidden from influencing any transition.**

**Interface contract:**
```
interface PoolPlugin extends Capability {
  provision(spec.pool, demand, orgBudget): void;   // minHot/maxHot/targetConcurrency/warm/coldStart
  route(request, spec.affinity): InstanceId;        // keys[tenant,conversation,entity]; prefer|require
  instanceState(id): "hot" | "warm" | "cold";       // telemetry ONLY
}
```
Rests on the **fungibility invariant**: no run-critical state lives in an instance
(Context, event history, HDC store, caches all live in the stator); any instance MAY
serve any run by loading the pinned `definitionVersion` and continuing/replaying from
recorded history — a K8s Deployment of interchangeable pods. `spec.pool` **declares**
warmth intent; the platform **provisions** against demand, **bounded by the §13.2 org
budget** (pre-warming is the first cost shed when the cap is spent). `spec.affinity`
declares `keys[tenant, conversation, entity]` + `mode: prefer` (soft, falls back to
any instance/cold) | `require` (hard pin, can starve throughput). Which instance
served and its warmth are **operational metadata** — `select_next`/`branch` MUST NOT
read them. Affinity propagates through sub-rotor handoffs but **never widens
authority.** The pool *mechanism* (HPA, Fly suspend/resume, hash ring) is
runtime-specific and MUST NOT appear in the portable document.

**Open basic implementation:** **no-op / single-instance** — cold-start every run,
route every request to the local instance, ignore `pool` + `affinity` entirely.
Still fully conformant, because determinism-neutrality guarantees identical runs,
only slower.

**glyphh premium swap-in:** a real pool manager — Kubernetes HPA / min-replicas, Fly
Machine suspend/resume, predictive autoscaling, and a consistent-hash affinity ring
over the fungible pod fleet — honoring the hot contract (model loaded, connections
open, caches primed, gateway connections established) and the budget bound while
keeping which/how-warm instance out of every predicate.

### 3.8 Capability negotiation

The contract that makes "build once, run anywhere" safe rather than lossy.

1. **A rotor DECLARES** required capabilities in the document — e.g.
   `spec.requires: { grounding: hard-gate, stator: pgvector, models: [frontier] }`,
   plus the implicit requirements of the steps it uses (a `gate` with
   `enforcement: hard` requires logit-access grounding; a `retrieve.vector` step
   requires an embedding-capable memory plugin).
2. **The runtime ADVERTISES** a capability manifest by folding each plugin's
   `status() → { ready, detail, tier }` into a document:
   ```
   { grounding: { ready, tier: "basic", detail: "exact-match; no hard gate" },
     memory:    { ready, tier: "basic", detail: "sqlite; no pgvector" },
     models:    { ready, tier: "basic", detail: "local lane only" },
     gateway:   { ready, tier: "basic", detail: "identity adapters" },
     ... }
   ```
3. **The runtime RECONCILES** at load, before any step runs:
   - **satisfied** → run normally;
   - **missing but degradable** → run with the documented graceful degradation
     (e.g. `enforcement: hard` requested but only soft grounding present → fall back
     to soft verify-then-refuse **and record that substitution in the run
     metadata**);
   - **missing and not degradable** → **clean refuse at load** with a typed error
     naming the missing capability (e.g. a rotor that requires the frontier lane on a
     local-only box, or requires `pgvector` for a tenant-scoped cache that SQLite
     can't share cross-pod).

The invariant: a missing premium capability yields **graceful degradation or a clean
refuse, never a silently wrong answer.** Degradation is always **recorded** so a run
carries proof of which tier produced it.

### 3.9 Tie to conformance levels

The plugin model maps onto the spec's conformance levels (SPEC.md §3, §18):

- **L1 / Core (Declarative)** — valid against the JSON Schema, typed I/O signatures,
  closed-catalog types; **no execution.** Only the parser/validator is exercised; no
  plugin needs to be `ready`.
- **L2 / Standard (Executable)** — one engine runs full step semantics + retry/catch
  + gates + mandatory loop caps + escalation + gateway (normalize/throttle/meter) +
  attention + the **trust layer enforced deny-by-default.** Requires *ready* grounding
  (basic ok), memory, models, gateway, governance plugins. Pool/affinity **not**
  required.
- **L3 / Full (Portable/Replay-Conformant)** — deterministic control plane,
  checkpoint+replay StepRecords, idempotent effects, recorded caching (never
  re-consulted on replay), `space_id` validation, run-pinning + patch gates, metered/
  bounded replay, recorded identity/grants/attenuation, and **passes the
  golden-transcript suite.** Requires a memory plugin with a durable, shared event
  history (SQLite passes single-node; cross-pod L3 needs the shared stator). Pool/
  affinity remain **optional** — they are determinism-neutral by construction.

`glyphh-rotor` is the seed implementation, **not privileged by the spec.** The
reference runtime targets L2 out of the box and L3 with a durable stator.

---

## 4. The reference runtime (Node/TypeScript)

The concrete build for **this repo.** Apache-2.0. It abstracts `hdc.map` — it **does
not practice the patent.**

### 4.1 Module layout

A pnpm/npm workspace under `reference/` (packages published under an `@rotorspec/`
scope):

```
reference/
  packages/
    core/                      @rotorspec/core        — engine, no I/O
      src/
        loader.ts              parse YAML/JSON → AST, resolve $refs/defaults
        validator.ts           JSON Schema (spec/schema/) + static graph checks (L1)
        engine.ts              the run loop (§2.2): resolve→redact→replay|cache|execute→record→merge→select_next
        select-next.ts         pure control-plane router (§2.4)
        step-record.ts         StepRecord shape, canonical hashing, idempotency_key
        context.ts             typed state + reducers (last-write-wins|append|merge|sum|max|min|union)
        frames.ts              frame stream types (propose|parse|dispose|backtrack|gate|assert|refuse|cache|delta|tool|done)
        capability.ts          Capability interface + manifest reconciliation (§3.8)
        determinism.ts         logical clock, canonical JSON, sorted-key iteration guards
        errors.ts              typed errors (E_UNTRANSLATABLE, E_UNMERGEABLE, refuse, ...)

    handlers/                  @rotorspec/handlers    — the 20 step handlers (§2.3)
      src/
        prompt.ts model.ts hdc-map.ts write.ts
        retrieve-sql.ts retrieve-kb.ts retrieve-vector.ts
        gate.ts assert.ts plan.ts branch.ts loop.ts parallel.ts
        wait.ts escalate.ts tool.ts transform.ts cascade.ts sub-rotor.ts fail.ts
        index.ts               Map<StepType, StepHandler>

    gateway/                   @rotorspec/gateway     — the transport boundary (§3.5)
      src/
        adapters.ts            in/out normalizers, identity default
        translate.ts           table-driven MCP↔anthropic↔openai↔internal
        prompt-cache.ts        breakpoint lowering (no-op default)
        rate-limit.ts          logical-clock queue/backoff (best-effort)
        meter.ts               usage/cost recording (local = free/no-op)

    plugins-basic/             @rotorspec/plugins-basic — the OPEN implementations (§3)
      src/
        grounding-exact.ts     ExactMatchGrounding + soft verify-then-refuse (NO patent)
        memory-sqlite.ts       SQLite stator: closed ops, lexical recall, entity graph, event history, result cache
        models-local.ts        local OpenAI/Anthropic-compatible lane + geometric-fluency ranker fallback
        connections-loopback.ts HandlerRegistry + ~30 substrate MCP tools + app stubs
        governance-local.ts     unenforced local ledger, deny-by-default defaults, free-tier license
        pool-noop.ts            single-instance cold-start

    registry/                  @rotorspec/registry    — plugin registry + binding
      src/
        registry.ts            bind capability name → implementation; build manifest
        negotiate.ts           reconcile spec.requires vs manifest (§3.8)

    cli/                       @rotorspec/cli          — `rotor` binary
      src/
        index.ts               arg parse → validate | run | repl
        repl.ts                interactive loop (shares the adaL/glyphh banner)
        banner.ts              shared identity banner

  spec/schema/                 (symlink to ../../spec/schema — the ONE JSON Schema)
  conformance/                 golden-transcript runner (§4.5)
    transcripts/               (definition + recorded inputs + recorded results) → expected transitions
    run.ts
```

`@rotorspec/core` has **no I/O** — it depends only on the plugin interfaces. Every
plugin is injected. That is what lets glyphh reuse *nothing* here yet conform to the
same contract: the contract is the interface, not the package.

### 4.2 Parser / validator

`loader.ts` parses YAML or JSON to an AST and resolves `$ref`s and defaults.
`validator.ts` validates against `spec/schema/` (the single JSON Schema shared with
the whole project — **the one source of truth**, §6.2) for **L1**, then runs static
graph checks the schema can't express: closed step catalog, every path statically
reaches a reserved terminal (`end` / `__fail__` / refusing `assert`), a **typed
state schema is present** (mandatory — fan-in is undefined without it), **every
fan-in key has a declared reducer** (a concurrent write to a reducer-less key is
`E_UNMERGEABLE`), `space_id` is declared, and **every `loop` carries
`max_iterations` + budget.** `rotor validate` exits non-zero on any failure with a
typed, located error.

### 4.3 The executor & step handlers

`engine.ts` is the loop of §2.2 verbatim. Each handler in `handlers/` implements one
`StepHandler`:
```
interface StepHandler {
  type: StepType;
  execute(step, input, grants, ctx): Promise<StepResult>;   // may stream frames
}
```
Handlers call **plugin verbs**, never each other, and never `select_next`.
`model.ts` is the only inherently stochastic handler; it calls `models.execute` and
records the result. `branch.ts` / `transform.ts` / `retrieve-sql.ts` are pure given
the store. `sub-rotor.ts` starts a nested run whose frames join the parent stream and
whose identity **attenuates**.

### 4.4 The gateway, plugin registry, REPL, CLI

- **Gateway** (`gateway/`) ships identity adapters + the translation table + no-op
  prompt-cache lowering + best-effort rate-limit/meter. An absent `spec.gateway` is
  the identity default.
- **Registry** (`registry/`) binds `capability-name → implementation` and builds the
  advertised manifest from each plugin's `status()`. `negotiate.ts` reconciles
  `spec.requires` at load (§3.8). Swapping in glyphh's plugins is a registry binding
  change — no engine edit.
- **REPL** (`cli/repl.ts`) is an interactive rotor loop that **shares the
  adaL/glyphh banner identity** so the open runtime and the product read as one
  family at the prompt.
- **CLI** (`cli/index.ts`) — the `rotor` binary:
  - `rotor validate <file>` — L1 parse + schema + static checks; exit code = pass/fail.
  - `rotor run <file> [--in k=v] [--principal ...]` — execute a rotor; stream frames;
    print the projected `spec.outputs`; write the StepRecord history.
  - `rotor repl` — interactive session against a bound runtime.
  - `rotor conformance [--level L2|L3]` — run the golden-transcript suite (§4.5).

### 4.5 Conformance harness

`conformance/` runs the **golden-transcript suite** (SPEC.md §18): each transcript is
`(definition + recorded inputs + recorded activity results) → expected transitions`.
The runner loads the definition, feeds recorded inputs and recorded `execute()`
results (so no live model is needed), and asserts the **sequence of control-flow
decisions** matches — proving the control plane is deterministic and replay-stable
**without** asserting model tokens. This is the artifact a *second* implementation
(glyphh, or a third party) runs to prove conformance.

---

## 5. glyphh as an implementation

The glyphh product (`glyphh-rotor`, Python, closed) implements the **same contract**
with premium plugins + hosting + governance. It is not a fork of the reference
runtime — it is an **independent implementation of the spec** that happens to seed
it.

### 5.1 Same contract, richer plugins

glyphh-rotor is already the plugin architecture: `Runtime` wires nine
lazily-constructed, `status()`-reporting subsystems (`engine, memory, models,
scheduler, store, cache, projects, control_plane, updates`) that degrade rather than
raise (glyphh-integration.md §1). Each RotorSpec step type names one subsystem verb.
glyphh binds the **premium** side of each capability in §3:

| Capability | glyphh premium plugin |
| --- | --- |
| grounding | patented HDC engine + encoder; hard `GroundedConstraint` logit mask |
| memory/stator | Postgres + pgvector, cascade consolidation, scaled 7×33 lattice |
| models | Slicer frontier metering lane + governed providers + logit access |
| connections | server-side governed connector catalog, encrypted write-only headers |
| gateway | full MCP↔API↔format table, provider-native prefix-cache lowering |
| governance | org roles/grants, credits ledger, Ed25519 license, routing/anomaly |
| pool/affinity | K8s HPA pool, Fly suspend/resume, consistent-hash affinity ring |

The patented HDC **method** sits behind `verify()` / `groundedFillers()` and is
never exposed by the interface — glyphh reveals *what* it guarantees (admit-only-
what-memory-returns, veto fluent falsehoods, refusal as terminal), not *how*.

### 5.2 The same `.rotor` runs on both

Because both runtimes implement the same interfaces and the same JSON Schema, one
document runs on either — capability negotiation (§3.8) reconciles the difference:

- On the **open runtime**: `gate enforcement: hard` degrades to soft verify-then-
  refuse (recorded); `models: frontier` on a local-only box either falls back to
  local or **cleanly refuses** if the rotor declared frontier as required;
  `spec.pool`/`spec.affinity` are ignored; the stator is SQLite.
- On **glyphh**: the hard logit gate engages, the frontier lane meters credits, the
  pool warms pods, the stator is Postgres+pgvector — **same document, same control
  flow, richer data plane.**

Nothing in the portable document names a backend, a pod, or a mechanism — the
determinism boundary and the fungibility invariant guarantee the *transitions* are
identical; only the plugin implementations bound behind the interfaces differ.

### 5.3 Deployment (§17)

glyphh runs a rotor as a **stateless, fungible runtime instance** — state in the
stator, the gateway as the only boundary — which makes it a clean **Kubernetes
Deployment of interchangeable pods.** The pool manager (§3.7) provisions hot/warm/
cold instances against demand, bounded by the org FinOps budget (pre-warm shed first
when the cap is spent), and routes over `tenant/conversation/entity` affinity keys.
A `wait`/`interrupt` checkpoints to the shared stator and resumes on **any** pod;
replay reconstructs a run from recorded history on a pod that never saw the original.
The open runtime's single-instance no-op pool is the same design with the pool
plugin set to one cold pod — conformant, just not scaled.

---

## 6. Build plan

A phased path that keeps the reference runtime in **lockstep with the JSON Schema**
at every step.

### 6.1 Phases

1. **Schema + parser/validator (L1).** Land `spec/schema/` (the JSON Schema) and
   `@rotorspec/core` `loader` + `validator` + the static graph checks. Deliverable:
   `rotor validate` passes the reference rotors in `rotors/` and rejects malformed
   ones. This is L1 conformance — no execution.
2. **Executor + basic plugins (L2, single-node).** Build the run loop
   (`engine.ts`, `select-next.ts`, `step-record.ts`, `context.ts` reducers), the 20
   step handlers, and the six basic plugins (`grounding-exact`, `memory-sqlite`,
   `models-local`, `connections-loopback`, `governance-local`, `pool-noop`). Wire the
   plugin registry + capability negotiation. Deliverable: `rotor run base.rotor`
   grounds/refuses correctly on a bare box.
3. **Gateway.** Add `@rotorspec/gateway` — identity adapters, the translation table,
   no-op prompt-cache lowering, best-effort rate limit + metering — and route every
   effectful handler through it, inside the checkpoint boundary. Deliverable: a
   `model` step's in/out is identical whether the lane is a local endpoint or a
   mocked frontier, and usage is recorded per StepRecord.
4. **REPL / CLI.** `rotor repl` (shared banner) and finalize the CLI surface (`run`,
   `validate`, `repl`, `conformance`). Deliverable: an interactive session that
   streams frames and prints projected outputs.
5. **Conformance tests (L3).** Build the golden-transcript harness and the transcript
   corpus; add durable, shared event-history support to `memory-sqlite` (and document
   the cross-pod L3 requirement for a shared stator). Deliverable: `rotor conformance
   --level L3` green — deterministic control plane, replay-stability, idempotency,
   recorded caching never changing a transition.

Each phase is independently useful and independently testable; L1 ships before any
executor exists, and L2 ships before L3 replay guarantees.

### 6.2 Lockstep with the spec's JSON Schema

The **single JSON Schema in `spec/schema/` is the source of truth** for both the
document shape and the validator; the reference runtime consumes it directly (via a
symlink under `reference/spec/schema`), never a hand-copied duplicate. Discipline:

- **Schema-first.** A new step field, gateway option, or `spec.requires` key lands in
  the schema **before** any handler reads it. `validator.ts` validating green against
  the schema is the gate.
- **Additive-optional versioning** (SPEC.md §16, CloudEvents discipline). New fields
  are optional; a run finishes on the `definitionVersion` it started with (run-
  pinning, §16.3); a mismatched version marker on replay **fails the task rather than
  diverging silently.**
- **Closed catalog is enforced twice** — the schema `enum`s the 20 step types, and
  `handlers/index.ts` maps exactly those 20. A CI check asserts the two sets are
  identical, so the code can never drift from the catalog.
- **Golden transcripts are the cross-implementation contract** — the same corpus runs
  against the reference runtime and glyphh-rotor; a spec change that breaks a
  transcript breaks *both*, surfacing the incompatibility immediately.

---

## References

- [SPEC.md](../SPEC.md) — the normative specification (§§ cited throughout)
- [concepts.md](concepts.md) — primitives, frames, the grounding law
- [execution-model.md](execution-model.md) — the run loop, worked traces
- [glyphh-integration.md](glyphh-integration.md) — the step-type → subsystem-verb
  wiring for the glyphh reference implementation
