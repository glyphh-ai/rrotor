# Recursive memory — reconstruction, not recall

**Status:** design doc / north star. The substrate is **shipped**: schema-on-write
(the enricher), neural embeddings, HDC algebra (`HdcSpace` bind/bundle/cleanup),
the grounding gate, and three-lane `recallContext` (`src/exec/recall.ts`). The
**memory-tree layer**, the **short-term → long-term fidelity ladder**, the **memory
rotor** (a constructor LLM), and the **predict-forward beam** described here are
**not yet built**. This doc is the target and the build order. See
[`hdc-resilience.md`](./hdc-resilience.md) for the recall substrate and
[`memory.md`](./memory.md) for what ships today.

---

## The idea in one line

Every LLM forward pass is a **cold start** — the model holds no state and rebuilds its
entire working understanding from whatever context we assemble. So memory's job is not
*recall* (fetch the right fact) but **reconstruction**: rehydrate a grounded working set
cheaply and reliably, every turn. Recall is an input; the **constructed prompt is the
product.**

## Why recall is not enough

Retrieval gets you facts *near the query*. It does not get you a **grounded working
set** — the active bindings, the current-task frame, the "where am I / what's next"
that lets a model reason fluently instead of from a bag of chunks. The evidence is
human: a context switch costs ~15 minutes not because the facts were lost (they weren't)
but because the **working set has to be re-grounded**. Retrieval is cheap; grounding is
the cost. And a stateless model pays that cost *every turn*, not a few times a day — so
flat top-k RAG re-cold-starts perpetually and drifts over a long horizon, while a
structured, grounded reconstruction lets each turn **warm-start**.

**The real KPI is time-to-ground, not recall@k.** No public benchmark measures it;
LongMemEval and LoCoMo are single-forward-pass QA (recall + one read). They prove the
substrate; they do not prove reconstruction under long-horizon load.

## The two clocks

There are two memory clocks and today we run one mechanism across both:

- **Short-term — a *fidelity* clock.** The model is still reasoning *inside* recent
  turns; it needs them nearly **raw**. High-fidelity, dense, verbatim, capacity-bounded.
- **Long-term — a *relevance* clock.** Old turns matter only as **extracted, addressable
  facts** — structured, embedded, prime-decomposed.

Reconstruct them as a **fidelity ladder**, not a window. A turn drops down the rungs as
it ages:

| Rung | Content | Clock |
| --- | --- | --- |
| **0 — raw buffer** | last **n+x** turns, **verbatim** | short-term (working memory) |
| **1 — gist** | just-slid-out turns, one-line summaries | consolidation bridge |
| **2 — fact tree** | fully extracted facts, embedded + prime glyph, **with confidence** | long-term (recall) |

The **consolidation boundary** is the seam between rungs: as a turn falls off rung 0,
run schema-on-write, write the fact to the stator with its embedding, its prime glyph,
**and a confidence score**. This makes the gradient ("turn 2 carries more of turn 1 than
turn 20 carries of turn 19") *intentional* instead of an accident of when extraction
happened to run.

> **The minimum viable fix (Phase 1):** every context = **n+x raw previous turns** +
> **a fact tree with confidence**. Everything below is how that tree grows and how it is
> reconstructed; the ladder itself is the near-term, shippable piece.

## Facts become trees; sessions saturate them

A single learned skill — "how to build a bike," a codebase — is **not one memory**. It is
a **compaction that points** to everything related. That is exactly HDC bundling. So
memory is a **forest**:

- A **node** carries: a `glyph` (the HDC **bundle** of its children — its semantic
  address), a `compaction` (an LLM summary = the warm-up view at that granularity), a
  `saturation` count (facts/sessions through it), a `confidence`, tree edges
  (parent/child), and **graph cross-links** (associative edges *across* trees — the
  bike → gears → physics pointers).
- **Leaves** are raw schema-on-write facts, each with a confidence. Leaves stay
  retrievable **even after** the parent compacts — the pointer never destroys the
  library.

Two opposite motions grow the forest:

- **Expand** (in-session, on-topic): the tree grows *structure*. The active node is a
  **moving cursor** descending from the matched root into finer nodes as the conversation
  gets specific, branching on sub-aspect. More turns on a topic push a path deeper.
- **Saturate** (between sessions / when a region cools): the tree compacts *upward* —
  bundle children into the parent glyph, regenerate its summary. 100 sessions on one
  codebase saturate that tree into a dense summarized view.

Sessions expand; consolidation saturates. **Expansion without saturation is a memory
leak; saturation without expansion is amnesia.** Tree shape therefore encodes depth of
engagement: a topic worked 100× reconstructs a rich, instant warm-start; a stub gives a
genuine cold start. **Warm-up cost ∝ 1 / tree-maturity** — the system *earns* its
warm-starts by living in a domain.

The fidelity ladder and the tree are the **same structure** seen two ways: the **hot
frontier node = expanded + rung-0 raw buffer attached**; as the cursor moves on, that
node saturates → its raw drops to gist → to facts. Short-term vs long-term is just
*where the cursor is now* vs *everywhere it has been*.

## The tree operations are HDC operations

The model is not invented; it is the HDC algebra rrotor already ships, lifted one level
from facts to trees:

| Tree operation | HDC op | What it does |
| --- | --- | --- |
| Structure a fact (who/what/when → slots) | `bind (⊗)` | role-binding — what schema-on-write emits |
| **Saturate** a node (fold sessions into a pointer) | `bundle (⊕)` | superposition; more children → richer prototype |
| **Reconstruct** a tree (expand pointer → subtree) | `cleanup` | nearest-neighbour recovery of a bundle's constituents |
| Query a node ("torque spec?" from the bike node) | `unbind` | pull one role back out of the compaction |

A tree node *is* a glyph that is the bundle of its children plus a text compaction.
Bundle is lossy-but-addressable — precisely "a compaction that points to related things
without being any single memory." The saturation grounds the recursion: compaction
terminates each level into a pointer instead of an infinite regress.

## Confidence

Every fact and node carries a **confidence**, and it is load-bearing:

- **Reinforced** by saturation and agreement (the same fact seen across N sessions →
  stronger; HDC reinforcement already does this at the vector level).
- **Decayed** by contradiction and age.
- **Drives reconstruction ranking** (surface high-confidence first), **contradiction
  resolution** (knowledge-update: higher-confidence / more-recent value wins at
  reconstruction time, not left to the reader), and **abstention** (low aggregate
  confidence → the grounding gate refuses rather than hallucinates).

## The three strata — recursive reasoning on reasoning

- **Crystallized reasoning** — the trees. Saturated nodes are past sessions' reasoning
  *frozen into structure*.
- **Constructive reasoning** — the **memory rotor**, reasoning *live* over that frozen
  reasoning to build the worker's prompt.
- **Task reasoning** — the **worker**, reasoning on top of what the constructor handed it.

Each stratum rests on the one below. It is **recursive because it closes into a cycle**:
the worker's reasoning saturates *back down* into the tree, becoming the crystallized
substrate the constructor reasons over next time. The top feeds the bottom — which is the
mechanical definition of learning, and, run a thousand times on one domain, is the
expert's tree.

## The memory rotor (the constructor)

Context construction is a **cognitive act with its own forward pass**, not a mechanical
tree-walk. Deciding what to surface, how to structure it by prime-role, which
contradiction to resolve, what to pre-warm — that is *judgment*. So it gets its own
model: a **memory rotor** spinning around the stator, whose single terminal output is the
worker's prompt. In rotor terms this is two rotors with the NL/cosine router between
them — an existing shape, not a bolt-on.

Design constraints, honestly:

1. **"The entire tree" survives only because of saturation.** You cannot hand a mature
   forest's full leaf set to any model. You hand it the **compacted** forest (pointers),
   after HDC tree-find has narrowed *which* tree. The memory rotor is therefore
   **agentic over the tree**: action space = *expand / collapse / search*
   (`unbind`/`cleanup` a node back into its subtree); output = the constructed prompt.
   The **worker can also call back** for expansion, so a bad construction is recoverable
   rather than a one-shot gamble.
2. **Two passes per turn is the cost; the per-turn delta is the mitigation.** Full
   construction only on **cold-start / topic-switch**; on-topic turns **patch** the
   standing prompt (saturate-back the last turn, nudge the frontier). The two models are
   asymmetric on purpose — a cheap fast constructor every turn, an expensive worker only
   when the task needs it (or the reverse).
3. **Determinism.** An LLM-authored prompt is stochastic — tension with the rotor's
   deterministic-control contract. Resolution is the RotorSpec one: the memory turn is
   **stochastic data, checkpointed at the boundary** (like embeddings). Checkpoint the
   constructed prompt → replay is stable. The constructor should emit **structured,
   provenance-tagged context** (which nodes it pulled, which roles it filled), not opaque
   prose — so it is checkpointable, auditable, and measurable.

The loop closes on a reward: the **worker's success (or prediction error) is the training
signal for the constructor** — the cold-start-recovery metric doubling as the reward.

## Prediction — the beam (later)

Each turn fills in two directions: **(a) saturate-back** the last turn into the current
node, and **(b) expand-forward** — predict the next task glyph, search the forest for it,
**pre-reconstruct** it before the turn asks. Prediction *and* search. Beam **width tracks
topical coherence**: on-topic → narrow confident beam, cheap pre-warm; a switch widens
it, spikes prediction error, forces a re-find. HDC makes a wide beam affordable — run it
in 2k-dim vector space for almost nothing, materialise only the top hypothesis through
the LLM. This is the last layer, built on top of everything above.

## Runtime — the brain as a supervised rotor loop

The memory rotor runs **only on the back path**. The instant the worker (e.g.
Fable) responds, the brain fires: schema-on-write over *both* the user turn and
the response, HDC-encode, compact the response, saturate-back into the tree, and
**pre-build + cache the next turn's system prompt**. It runs in the shadow of the
response stream plus the human's read/think time, so the user's clock only ever
holds the worker.

- **The foreground has no LLM.** The critical path is two non-generative ops: drop
  the user's prompt into its (untouched) slot, and a deterministic `recallContext`
  for any delta the cached prompt didn't anticipate. The whole system prompt is
  already cached and waiting; the only holes are the slot and the recall delta.
- **State lives in the plane, not the process.** The cached prompt is a
  **materialized view** over the durable stator (facts, HDC, tree). Nothing
  load-bearing sits in a worker's RAM — so a crash reattaches and rebuilds the
  view, and a desktop↔cloud move just points the worker at the same plane.
- **Never-halt + restart survival = event-sourced queue + idempotent ops.** A
  durable warming queue plus the append-only event log (§5.4). A crash mid-warm
  resumes by replay; schema-on-write is idempotent-by-key, HDC bundling is
  deterministic, compaction is content-hashed — so re-running a half-done job is
  safe. The cached prompt, if lost, rebuilds from the log.
- **It's a rotor loop, not a new engine.** A supervised daemon
  (`ensureLocalRuntime()` on desktop, a pod-side worker in cloud) runs the warm
  loop: deterministic control (drain queue → write → HDC → compact → materialize),
  stochastic data (the assembler LLM) checkpointed at the boundary.
- **Cloud↔desktop portability** is the one real decision: (a) cloud
  source-of-truth, workers on either surface hit it (simple; network on the back
  path is fine, it's off-clock); (b) a local replica synced via the event log
  (offline/fast; more complexity). Start (a), earn (b).
- **The recall floor de-risks all of it.** If the brain is restarting, migrating,
  or behind, the foreground still works — it falls back to deterministic recall and
  runs *colder*, not *broken*. "Never halt" is a warmth goal, not a correctness
  requirement; the recall net catches both per-turn misprediction and brain-down.

## The memory rotor is an agent — loop + tools + skills + two models

The back-path rotor is not a single assemble() call; it's a **tool-using, skill-driven,
multi-model agent loop** — which is exactly what rrotor already runs. v0 (one Haiku
call) is the seed; production is a proper memory agent:

- **A memory tool-pack** (its action space over the stator/tree): `recall`, `writeFact`,
  `embed`, `hdcBind/bundle`, `treeExpand/saturate`, `readNode`, `cachePrompt`. The agent
  *decides and acts*, it doesn't just emit text.
- **Domain skills — the "swivel."** It detects the domain and loads the matching memory
  strategy: a **code-memory** skill (AST → structural tree, store signatures/decisions/
  flaky-notes, recall = code-tree-find), a **narrative-memory** skill (characters, plot
  threads, continuity), a **campaign-memory** skill (audience, brand voice, what worked).
  Each skill packages *what to store, how to structure the tree, and how to recall* for
  its domain. Skills are role-governed and shippable — a baseline set ships, orgs add
  their own (same governance as the standards forest).
- **Two (multi) model roles.** A **reasoning** model (the configured/metered assembler,
  e.g. Haiku) *judges and orchestrates* — domain? worth storing? how to structure? which
  skill? — and does the final prompt engineering. An **encoding** model (e.g. qwen-14b in
  prod, local/cheap, high-volume) does the mechanical work — schema-on-write extraction,
  embeddings, HDC. We already have both roles (enricher = encoder, rotor = reasoner); the
  elevation is the reasoner **orchestrating** the encoder through tools, in a loop.

The loop has three competencies, all off-clock on the back path:

1. **Swivel-store** — domain-detect → skill → decide what/how to store → encoder + HDC/
   embed tools write facts and saturate the tree.
2. **Recall + fact-tree assembly** — query, compose the relevant tree, cache it.
3. **Prompt engineering** — produce the kick-ass cached system prompt.

**It is a rotor — a RotorSpec document, not a new engine.** So it inherits, by
construction, everything a rotor gives: the deterministic-control / stochastic-data
split (the loop is deterministic; the reasoner + encoder outputs are checkpointed at
the boundary → a memory construction **replays identically**), event-sourced
resumability (the restart / cloud↔desktop durability is a rotor property, not bespoke
plumbing), HDC grounding, the gateway/attention routing between the two models, and
governed skills + tools behind the permission mode. The "build" is therefore mostly
**authoring a rotor** — a control graph (swivel → store → recall → assemble → prompt),
skill hooks (domain playbooks), the memory tool-pack, and the two model roles — on the
reference executor we already have.

Honest bounds: an agentic loop makes more model calls than one assemble() — fine on
latency (off-clock) but it must be **metered per call** (it already is; see below) and
**bounded** so it doesn't over-store: confidence + prediction-error gates decide what's
worth keeping, a storage budget caps writes, and the skill constrains the strategy so
"swivel" is guided, not free-form.

## Three loops — attention is the point

The memory system is **three loops**, and attention is not a peer of the other two —
it is the **purpose**; recall and the governor serve it. Formally (see the Markov
note below), the constructed context is a **sufficient statistic** `s_t` of the
conversation, and the three loops maintain its three parts:

| Loop | Maintains (of `s_t`) | Cadence | Model tier | Job |
| --- | --- | --- | --- | --- |
| **Attention** | the **goal / focus** — the invariant the trajectory serves | **every forward pass**, out of the recall chain | cheap (HDC-alignment / tiny) | hold the one goal, re-anchor the worker to it, assemble goal-first |
| **Memory / recall** | the **knowledge** — relevant facts/tree | per-turn, back-path | qwen encode + Haiku assemble | observe `(prompt, response)` → extract, and retrieve for the focus |
| **Governor / dream** | the **structure** of knowledge — the tree | rare, offline | Fable (the dream loop) | consolidate, dedup, re-saturate, resolve contradictions |

**Attention makes `s_t` *sufficient* instead of drifting.** Buried inside recall,
breadth drowns focus; as its own always-run loop with a single job, the goal never
dilutes — the salience network, not the hippocampus. It **consumes** from recall
(facts) and from the governor's tree (structure), and it **drives** the assembly:
goal → where-we-are → the minimal memory that serves the goal. Recall and governor
exist so attention has something clean to focus with.

**Two planes feed attention each turn:** the *working plane* (goal + recent raw
prompts+responses — the rotor must see BOTH sides of the exchange) and the *memory
plane* (the recalled tree). Attention assembles the next prompt from both.

## The third tier — the consolidation governor (the fact-tree cleaner)

The per-turn memory rotor is, ironically, a **rotor with no memory of itself** — it
reasons *locally*, about this turn's frontier, with no global view of how the tree is
aging. So the tree accretes cruft: duplicate facts, stale low-confidence leaves,
regions expanded but never saturated, contradictions resolved locally but never
globally. The fix is the recursion made real — **a loop that governs the memory
loop**: a slow, expensive, high-judgment pass (Fable-level) that runs **rarely**
(every ~N turns, or drift/entropy-triggered, or on idle) whose only job is to **clean
the overall fact tree** — prune, dedup, re-saturate, merge/split nodes, resolve
contradictions globally, recompact.

This is **sleep consolidation.** The awake tiers perceive and act; the governor is the
offline systems-consolidation pass (hippocampal replay) that reorganizes and
strengthens memory when the task isn't looking. Three tiers, by rate and judgment:

| Tier | Model | Cadence | Job |
| --- | --- | --- | --- |
| **Encoder** | qwen-14b | per-turn, high-volume | schema-on-write, embed, HDC — mechanical |
| **Rotor** | Haiku | per-turn, back-path | construct the prompt, store this turn, recall — tactical |
| **Governor** | Fable | rare (~10 turns / drift / idle) | global tree hygiene — strategic |

Cheaper + faster + more frequent at the bottom; expensive + slower + rarer + smarter
at the top. The governor is **also a rotor** (deterministic; tools: `prune`, `merge`,
`split`, `reSaturate`, `dedup`, `reEmbed`; skills: per-domain tree-hygiene). And the
recursion **grounds out here** — the governor's job is bounded and rare, so there is no
fourth tier; **encode → construct → consolidate** is the natural termination, the same
way the brain has no infinite meta-sleep. Trigger it on tree *drift* (too many
low-confidence or duplicate leaves, an unsaturated over-expanded region), not a rigid
clock — turn-count (~10) is just the simple default.

Billing is unchanged: a governor pass is a Fable-priced `MemoryUsage` record
(`phase: "govern"`) under the same **memory-rotor** service line — rare, so bounded
despite Fable's rate. The multi-tier reality needs no new plumbing.

## Production — optional, metered, governed

Memory is **opt-in per rotor** (`memory: { enabled, model }`). When off, the rotor
runs bare; when on, the back-path assembler is billed.

- **Metered at the source.** Every assembler call emits a `MemoryUsage` record —
  `{ service: "memory-rotor", phase: "warm"|"construct", model, inputTokens,
  outputTokens, sessionId }` — via an injected `onUsage` sink. rrotor reports
  **tokens + model id only; it never prices.** The host applies the governed rate
  card for `model` and attributes the credit burn to the org/user. `service:
  "memory-rotor"` keeps it a **distinct, reportable line** from worker turns, so
  memory spend is traceable and auditable on its own.
- **Governed by the existing model catalog.** The assembler must be a model in the
  **super-admin catalog with a rate card** (same supported vendors we configure
  today; the rotor's Anthropic + OpenAI-compatible transport already covers them).
  The chain: super-admin catalog + rate cards → **org admin sets the org's memory
  model** (from the org's allowed list) → optional **user override** within
  org-allowed models → the surfaces expose the picker. No new pricing plumbing —
  it reuses model governance and credit attribution.
- **Surfaces:** desktop, web, and mobile each get a memory-model config (org-admin
  default; user override where the org permits).
- **Economics make premium viable.** Because the assembler is **off the user's
  latency clock**, a slower/stronger model is affordable — an org (or user) can pay
  for a **Fable-powered** memory rotor if the reconstruction quality is worth the
  burn; Haiku is the sensible default, and the cached standards prefix keeps the
  per-turn cost low. Cost-per-turn and cache-hit rate, not speed, are the levers.

## What exists vs. what's new

**Shipped:** schema-on-write facts, `HdcSpace` bind/bundle/cleanup, the grounding gate,
NSM primes / `decompose`, three-lane `recallContext`, the stator, the retry-hardened
HTTP embedder. **Memory rotor v0** (`src/harness/memory-rotor.ts`): sliding fidelity
buffer + recall + a cheap assembler → the worker's `Look here / Do this / Don't do that`
system prompt, with a per-call `MemoryUsage` metering record (tokens + model, host
prices via the rate card).

**New:** the node/forest schema (glyph + compaction + saturation + confidence + edges);
saturating consolidation (bundle children → parent, compact upward); the **rung-0 raw
buffer + fidelity ladder**; tree-find on cold start; tree-reconstruct (prime-organized
warm-up); the **back-path warm loop** (supervised, event-sourced, resumable); the
**predict-forward beam**; and the **host wiring** — org-admin memory-model governance,
credit metering/reporting, and the desktop/web/mobile config.

## The falsifiable first experiment

The vision is complete; the elegance is a **hypothesis** until one curve bends:
**cold-start recovery.** Interrupt an agent mid-task, resume it, and measure
**turns-to-full-productivity** (and quality lost along the way) across three arms —
**no memory / flat RAG / rrotor** — on a real long-horizon task (e.g. a codebase over
hundreds of turns). That is the human's 15 minutes turned into a number, and it is the
metric no leaderboard measures. Everything here is built to bend it.

## Build order

0. **Memory rotor v0 — DONE.** Assembler → worker system prompt, sliding fidelity
   buffer, recall, `MemoryUsage` metering. (`src/harness/memory-rotor.ts`.)
1. **Short-term → long-term fix (the ladder).** Rung-0 raw buffer of **n+x** turns +
   the **fact tree with confidence**; consolidation boundary on slide-out. *Shippable on
   its own; measurable warm-start.*
2. **Back-path warm loop.** Move the assembler off-clock: a supervised rotor loop that,
   after each worker response, observes the exchange and pre-builds + caches the next
   system prompt. Foreground = cached prompt + deterministic recall delta + verbatim slot.
   Durable warming queue + event-sourced resume.
3. **Host wiring (production).** Opt-in rotor config (`memory: { enabled, model }`); the
   `onUsage` sink into credit metering with a distinct **memory-rotor** service line;
   org-admin memory-model setting governed by the super-admin catalog + rate cards;
   desktop/web/mobile config; usage reporting. *Reuses model governance — no new pricing.*
4. **Forest layer + saturating consolidation.** Node schema, expand/attach + bundle-up,
   compaction, cross-links. *(Moves the multi-session / knowledge-update numbers.)*
5. **Cold start: tree-find + tree-reconstruct.** HDC/graph lookup → prime-organized warm-up.
6. **Code-tree track.** Deterministic Layer-1 source map + experiential Layer-2 overlay.
7. **Per-turn saturate-back, then the predict-forward beam.**

## Open decisions

- **Memory rotor cadence:** every turn (patch) vs cold-start + *detected* switch (trigger
  on beam-width / prediction-error threshold). Sets the whole cost/latency curve.
- **Saturation / split-merge thresholds:** by fact-count, glyph-drift, or confidence?
  Wrong thresholds degenerate the forest into one blob or a million singletons.
- **Compaction cadence:** lazy (on read / on threshold) — never every turn (LLM cost).
- **Confidence dynamics:** reinforcement and decay rates; how contradiction re-weights.
- **Determinism envelope:** exactly which artifacts are checkpointed (constructed prompt,
  compactions, tree mutations) so replay holds.
