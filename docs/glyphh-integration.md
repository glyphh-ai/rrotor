# glyphh Integration

How a RotorSpec step invokes the **glyphh** substrate — the HDC encoder that maps
NL → sim (roles / segments / layers / cortex), the vector / KB / SQL retrieval
paths, and the escalation lanes — and how **glyphh-rotor**, the reference
executor, runs a Rotor Document.

For the primitives themselves see [concepts.md](concepts.md). This document is the
wiring diagram between the spec's step types and the runtime's subsystems.

---

## 1. The runtime a rotor executes on

`glyphh-rotor` is a Python package whose `Runtime` wires nine lazily-constructed,
gracefully-degrading subsystems: `engine`, `memory`, `models`, `scheduler`,
`store`, `cache`, `projects`, `control_plane`, `updates`. RotorSpec is the DSL
those subsystems execute. Every subsystem answers a `status() → {ready, detail}`
contract and degrades (`ready: false`) rather than raising — so a rotor runs,
partially, on a bare box with no Postgres, Redis, local model, or MCP SDK.

Each step type in [SPEC.md §7](../SPEC.md) names a specific subsystem verb:

| Step type | Subsystem / entry point |
| --- | --- |
| `hdc.map` | `encoder/` — `Encoder.encode` |
| `write` | `memory` — `tell_raw` / `remember` / `absorb` |
| `retrieve.sql` | `store` — `sqlite_store.execute_op` (closed ops) |
| `retrieve.kb` | `memory` — `FactMemory.probe/verify`, `EntityGraph` |
| `retrieve.vector` | `memory` — `SemanticVectors` / `semantic_recall` |
| `gate` (hdc-ground) | `engine` — `FactMemory.verify` / `GroundedConstraint` |
| `model` / `escalate` | `models` — `Slicer.decide` (local / frontier lanes) |
| `plan` | `models` — `SchemaGuard` → typed `Plan` → deterministic execute |
| `tool` | `mcp/` (in-runtime) or `attach/` (client app methods) |
| `cascade` | `memory/cascade.py` — short → mid → long consolidation |

---

## 2. NL → sim: how a step invokes the encoder

The encoder (`glyphh_rotor/encoder/`, pure numpy, boots on a bare box) is where
"NL → sim" happens. Two directions, both driven by rotor steps.

### 2.1 Write direction (`write` / `hdc.map`)

```
NL text
  → enricher (enrich/heuristic.py | local_enricher.py, auto_enricher())
  → {layer:{role:value}}                      # structured facts
  → _sanitize_universal(facts)                # drop off-schema layers/roles + empty/none/null/n-a  (structural ∅)
  → universal_role_fillers(facts)             # flatten → [(layer.role, value)]   ← the NL-fact → hypervector bridge
  → Encoder.encode(role_fillers)              # cortex = bundle over bind(role_vec, filler_vec)
  → cortex (bipolar hypervector)
```

- A `write` step with `mode: absorb` runs this whole pipeline at write time and
  persists (optionally keyed for a versioned `is_current` chain). `mode: raw`
  (`tell_raw`) skips the enricher — the facts are already structured, **no model**
  involved.
- A `hdc.map` step runs the same pipeline **without persisting** — it hands back
  the `cortex` and `slots` for ad-hoc compare/verify.

### 2.2 Read direction (`retrieve.kb` / `gate`)

```
NL question
  → (entity, role)                            # a retrieve.kb / gate step REQUIRES both
  → unbind(cortex, role_vec(role))            # = bind, since bipolar bind is self-inverse
  → cleanup over the per-role codebook        # nearest known filler
  → grounded filler  (+ membership, margin, top)
```

`verify(entity, role, filler, margin=0.05)` returns
`(grounded, membership, margin, top)`. Admission (in `forward_hdc.py`) requires
`membership ≥ ADMIT (0.10)` and `margin > MARGIN (0.05)` and cleanup-winner ==
filler. This is the `gate` step's `mode: hdc-ground` config, verbatim.

### 2.3 Roles / segments / layers / cortex, concretely

- **Roles** are the 33 `layer.role` slot keys the encoder binds against
  (`role_vec("perceptual.color")`). A slot picker in a `write`/`gate` step chooses
  one from the fixed enum.
- **Layers** are the 7 groupings (`entity`, `perceptual`, `spatial`, `temporal`,
  `relational`, `quantitative`, `epistemic`). In `HierMemory` they are *also*
  addressable subtree keys: `layer_query(entity, layer)` extracts a subtree.
- **Cortex** is the resulting bipolar hypervector for an entity/fact —
  `Encoder.encode(role_fillers)`.
- **Segments** are **not** a store. Segmentation is per-token slot tagging: the
  role-head labels each surface word as a content slot (`layer.role`) or a
  function word (`O`, from `ROLES = ["O"] + ALL_ROLES` in `corpus.py`). A rotor
  never allocates a "segment" object; it picks a slot.

### 2.4 The space invariant a step carries

`space_id = compute_space_id(vector_dim, encoder_seed, roles_config) = sha256(...)`.
`grounding.assert_space()` refuses to bind across mismatched spaces. Every
`hdc.map` / `write` / `retrieve.*` / `gate` step **inherits `spec.space`** (or pins
its own) and validates the `space_id` before binding. Atoms are deterministic per
`(name, seed)`, so the same fact always lands in the same place — the root of the
gate's reproducibility.

---

## 3. Retrieval: which step hits which path

Four deterministic retrieval paths, each a distinct step config:

### 3.1 `retrieve.sql` — closed ops (the authority path)

`sqlite_store.execute_op` runs one of a **closed** op set over the indexed
`fact_slots` table: `lookup / prev / count / count_not / top / who / compare /
refuse`. Conditions are `{layer.role: value}`, intersected via **fixed SQL
templates** (`INTERSECT`). There is **no model-generated SQL** — runaway queries
are excluded by construction, and this is a selling point, not a limitation.
Versioning is via `key → is_current` supersession.

**Exact aggregates come only from here.** A rotor **MUST NOT** decode numbers from
vectors — vectors are lossy at arithmetic. `total / average / count` run over the
exact store (`business.py Org`).

### 3.2 `retrieve.kb` — associative + graph

- **HDC probe/verify** — the entity-keyed law of §2.2.
- **EntityGraph** — `node(entity)` (union of an entity's slot fills) and
  `neighbors(entity)` (entities sharing a `(layer, role, value)` = a weighted
  edge).
- **Multi-hop / analogy** (`HierMemory`) over the tree `cortex → layer → role →
  filler`: `chain(start, [role1, role2, …])` with cleanup (SNR reset) per hop;
  `analogy(a, filler, b, layer)` *discovers* the relating role. Configured as a
  `retrieve.kb` step with a role-**path**.

### 3.3 `retrieve.vector` — semantic

`SemanticVectors` + `semantic_recall`: embed the query (nomic, OpenAI-compatible
endpoint), brute-force unit-dot rank over stored turns/events. Thresholds `> 0.35`
semantic, `> 0.05` lexical. The *embedding* is the stochastic boundary and is
checkpointed; ranking over recorded vectors is deterministic.

### 3.4 Storage backends

`MemorySubstrate` is dual-mode: **SQLite** by default (`~/.glyphh/rotor.db`,
stdlib, zero-dep) or **Postgres + pgvector** when `DATABASE_URL` is set. A rotor
step is written against the verbs, not the backend; a missing Postgres degrades to
SQLite, not a crash.

---

## 4. The model & escalation lanes

`models/slicer.py` `Slicer` is an Anthropic-compatible `/v1/messages` gateway.
`decide(body, picked_route)` routes each turn onto exactly two lanes:

- **LOCAL** — translated to `llama-server` (Anthropic wire format). **Free by
  construction.** Local inference **never** proxies the frontier gateway.
- **FRONTIER** — the bundled turn is forwarded verbatim to the governed gateway.
  **Credits burn only here** — the metering point.

Together with adaL's `/v1/messages` proxy (the format/protocol translator between
provider wire formats and internal I/O), the `Slicer` is the reference
implementation of the RotorSpec **gateway** layer ([SPEC.md §8](../SPEC.md)):
rate limiting, the `local`/`frontier` lane split, `MCP ↔ API ↔ format`
translation, and the metering point all live here. The attach `HandlerRegistry`
(§6) is the gateway's MCP/app adapter table.

A `model` step picks `lane: local | frontier`. An `escalate` step encodes the
ladder:

- **`FrontierDeclined(402/403/429)`** is a **governance** decline (credits, role,
  rate) — distinct from transport failure — and **falls back to local** so the
  user keeps working.
- The `local → frontier → human` ladder: local propose is the default; a
  refuse/empty-cell or low-margin is the human rung (surface "I don't know.");
  frontier is the paid middle rung, gated by governance.

### 4.1 The "language vs authority" split (`plan` step)

`plan` composes three runtime pieces so the model does language while the lattice
keeps authority:

1. `SchemaGuard.classify(question)` — nearest-prototype semantic **router** with
   an explicit `OUT_OF_SCHEMA` negative class; route by **margin** (`THRESHOLD =
   0.05`), else refuse.
2. `Plan(op, field, tier, region, k)` — a typed DSL whose slots are constrained to
   schema enums (`OPS: total / average / count / churn / unsupported`;
   out-of-schema → refuse).
3. `execute(org, plan)` — runs **deterministically** over the exact store (exact
   sum/avg/count; `churn_cohort` via HDC cosine to a prototype).

---

## 5. The hard gate at execute time

`engine/lattice_adapter.py GroundedConstraint` is a prefix-automaton **logits
processor**: given the tokens-so-far it masks the vocabulary to **only** the token
continuations of the store's `grounded_fillers(entity, role)` — or a refusal
token. The model *cannot* emit a non-grounded token. "The guarantee lives in the
wrapper, not the weights."

- `adapters.make_constraint(store, tokenizer, prompt, entity, role, refusal,
  eos_id, space_id)` + `build_allowed()` build it.
- `hf_processor` / `mlx_processor` wrap it per runtime (MLX-LM / HF / vLLM share
  the `(tokens, logits) → logits` callable; llama.cpp gets a GBNF grammar).
- `engine/grounding.py` abstracts `LocalStore` (runs here) vs `RuntimeStore`
  (targets the production glyphh store: exact `semantic` symbolic side =
  authoritative + `cortex` HDC bytes = geometric verify).
- `engine/adal_sdk.py RotorSDK` is the one-facade bolt-in: `ingest(records)` →
  both structures; `probe / verify / grounded_fillers / aggregate`;
  `constraint(...)` the logits processor.

A `gate` step chooses `enforcement`:

- **`hard`** — the `GroundedConstraint` logit mask (requires logit access).
- **`soft`** — verify-then-refuse post-hoc, for API models without logit access:
  generate, parse the asserted `(entity, role, filler)`, `verify()` each,
  refuse/repair on a miss.

---

## 6. Tools: two flavors, one registry

`tool` steps come in two flavors, both dispatched through the same
`HandlerRegistry {method: handler}` table (`attach/registry.py` — `dispatch`
returns `ok/error` frames and never raises; `invoke` raises):

- **`tool.mcp`** — one of the ~30 in-runtime substrate tools (`think / ask / tell
  / remember / tell_raw / recall / history / query / keys / browse / entities /
  similar / drift / merge / inspect / amend / archive / forget / stats /
  consolidate / …`), each carrying a JSON `inputSchema`. The model decides *what*;
  the tool guarantees *how*. `create_mcp_server(runtime)` binds them; import is
  safe without the MCP SDK (lazy).
- **`tool.app`** — a client/app method over the **loopback** attach channel:
  `panels.open/switch/close`, `layouts.open`, `apps.listTools/callTool/install` —
  the side-effectful "skill hooks." A live Electron client overrides these stubs
  to drive real windows/apps.

Attach is **loopback-only** by security decision — the old cloud reach-in was
removed; nothing dials into a personal machine. App/skill hooks dispatch locally
through the channel.

---

## 7. How glyphh-rotor executes a Rotor Document

The attach `run(prompt)` handler is the entry point that drives a whole rotor
pass. `install_runtime_handlers` streams the engine's frames as `delta / tool /
done` events. Concretely, for a macro rotor pass:

1. **Load & pin.** Parse the Rotor Document, validate against the JSON Schema,
   resolve `spec.space` → `space_id`, pin the run to `definitionVersion`.
2. **Walk the graph.** Run the loop of [execution-model.md §1](execution-model.md):
   resolve inputs → (replay recorded output | execute the subsystem verb) →
   checkpoint a `StepRecord` → merge into Context → select next.
3. **Stream frames.** Each step yields typed frames (`propose / parse / dispose /
   backtrack / gate / assert / refuse`); the attach channel relays them as
   `delta / tool / done`. A `gate` step may run the micro rotor
   (`RotorEngine.run`) internally, streaming its per-word frames.
4. **Ground & gate.** `retrieve.*` and `gate` steps validate `space_id` and hit
   the exact subsystem (`execute_op`, `FactMemory.verify`, `GroundedConstraint`).
5. **Escalate on trigger.** A refuse / low-margin / frontier-decline routes
   through `Slicer.decide` up the `local → frontier → human` ladder, degrading to
   the fallback lane rather than crashing.
6. **Consolidate.** Out of the request path, `cascade` runs `short → mid → long`
   (verbatim turns + vec → local-model event summaries → 7×33 lattice facts via
   `absorb`), non-blocking, waiting when the local model is down.

Because one step's frame stream feeds the channel the next step reads, **rotors
compose by piping frames** — a `sub-rotor` call is just another `run` whose frames
join the parent's stream, and the whole thing remains replayable from the event
history.

---

## 8. The trust layer on the glyphh substrate

The trust layer ([SPEC.md §11–§14](../SPEC.md)) is not new runtime — it is the
**declare / grant / enforce** discipline wired onto glyphh primitives that already
exist. The rotor document carries only the **declaration**; the **grant** lives in
the glyphh org/policy plane; the runtime and gateway **enforce**.

| Dimension | Declared in the document | Granted / enforced by (glyphh reference) |
| --- | --- | --- |
| **Identity** (§11) | `spec.identity` — principal kind + required scopes | **device-auth + license** — the platform session / JWT authenticates the principal; the license/role plane resolves granted scopes. Both identities land in each `StepRecord`. |
| **Instruction boundary / input anomaly** (§12.2, §14.2) | `spec.assurance.anomaly.input: { mode: firewall }` | **firewall-scanner** — scans prompt + connection/stator content for injection/jailbreak before a `model` step; a hit is a first-class anomaly, not a log line. |
| **Secrets isolation** (§12.3) | a step names a **connection**, never a secret | **custom-MCP / connector** model — connection headers are stored **write-only + encrypted** and executed **server-side** by yo-server; the secret never enters model context or a `StepRecord`. |
| **Control policy** (§13.1) | `spec.policy.requires` + `approval_gates` | **capabilities / guardrails** — the rotor's declared capabilities are matched against granted guardrails at load; a denied primitive is a governance decline, and approval gates reuse `gate`/`wait`. |
| **Data-access** (§13.4) | `spec.access` — connector/operation/**field** + stator spaces/roles | **org_roles + connector governance** — grants scope reads to fields/rows and to memory spaces/entities/roles; the retrieval + gateway boundary **redacts ungranted fields before context** (a marketing rotor never receives the finance space). |
| **Budget governance** (§13.2) | attention/loop budgets (a run's ceiling) | **credits / metering** ledger — the `Slicer` frontier lane is the metering point; the org cap spans every rotor, and its exhaustion fires `budget-exceeded` for all of them. |
| **Routing governance** (§13.3) | model/lane requirements | **`Slicer.decide`** — the gateway enforces which lanes/providers a principal may route to (PII → local-only); the router only proposes. |
| **Output anomaly / grounding** (§14.2) | `spec.assurance.anomaly.output` incl. `hdc-ground` | **`FactMemory.verify` / `GroundedConstraint`** — an ungrounded assertion *is* an anomaly: "the model asserted what memory did not return" becomes a deterministic refuse. |

The enforcement points sit exactly where the run loop already has boundaries
([execution-model.md §6](execution-model.md)): identity at run start, policy before
a step, redaction at `resolve`/retrieval, the firewall gate before a `model` step,
and the anomaly/ground gate after it. Deny-by-default throughout — an ungranted
scope, route, or field is refused, never assumed, and a rotor can never widen its
own grant.
