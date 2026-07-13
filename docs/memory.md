# Memory: what it is, what's proven, what's bounded

OpenRotor's memory is **model-independent and vendor-portable by construction**: it
reads and writes only the stator (facts + recorded turns), never a model. A
directive given at turn 1 with one provider is recalled and injected at turn 20
with any other provider — Claude, an OpenAI model, or a local one — because neither
storage nor recall touches the generator.

This document reports what we validated, the reliability envelope, and the
keep/cut decision for each layer. Tests: `test/memory/`.

---

## Scoping: runtime vs. session vs. run (where memory lives)

The mental model: a **runtime instance** is the long-lived thing (the user's machine,
or a cloud instance with a git remote). One runtime instance ⇒ **one stator**. Many
rotor **definitions** (base, super-power, web-dev…) run against that one stator. A
rotor is a *path*; the stator is the *memory*.

| Scope | What | Keyed by | Lifetime |
| --- | --- | --- | --- |
| **Run** | the event history (StepRecords) — the replay tape | `run_id` | one run (prunable/archivable) |
| **Runtime / lifelong** | facts, directives, turns, HDC space | `(entity, role, space_id)` | the runtime instance's life |
| **Definition** | the rotor spec | `namespace/name@version` | lives in the definition registry, not the stator |

So, concretely:

- **Facts span everything in a runtime** — every run, every definition. `base` for 10
  prompts then `super-power` for the next both read/write the same stator; the
  super-power run recalls what base wrote (`continuity.test.ts`).
- **No throwaway per-session SQLite for facts** — that would orphan lifelong memory.
  The stator persists for the runtime; only the run *tape* is per-run.
- **`run_id` content-addresses the rotor identity** (`namespace/name@version` + inputs),
  not just the version — so many definitions sharing one stator never collide tapes.

### Where the stator lives

The runtime is **stateless compute** (§17.1): it holds no run-critical state and
points at an external stator. So "the runtime is spun up per session" is fine — the
memory isn't in the runtime.

- **Local machine:** the stator is a durable **SQLite file** on disk
  (`ROTOR_STATOR_URL=/path/rotor.db`). Survives runtime restarts → lifelong.
- **Cloud instance:** the container filesystem is ephemeral, so the stator MUST be on
  durable storage — a mounted/networked volume for SQLite, or **Postgres + pgvector**
  (the premium stator) reached over `ROTOR_STATOR_URL`. The per-session runtime
  connects to that durable, persistent store.
- **Tenancy:** one runtime instance = one user ⇒ the stator is single-tenant, so no
  row-level tenant scoping is needed. (A *shared* multi-tenant runtime would need a
  tenant key on facts — not this model.)

---

## The two modes (they are different, and the distinction matters)

| Mode | Example | Mechanism | Recall trigger |
| --- | --- | --- | --- |
| **1. Standing directives / facts** | "always use tabs", "my name is Ada" | extracted to durable facts (`write` absorb), **always injected** | unconditional |
| **2. Semantic recall** | "what did we decide about the DB?" | cosine over the embedding of recorded turns (§7.7) | similarity |

The headline requirement — *"I told you to always do X at turn 1, recall it at turn
20"* — is **mode 1**, not mode 2. At turn 20 the current query shares no words with
"always use tabs", so similarity would miss it. Directives must be **extracted once
and always injected**, which is exactly what the fact store + `assembleRecall` do.

### The RECALL assembler (`src/exec/recall.ts`)

`assembleRecall(memory, query)` returns `{ directives, recalled }` and `recallBlock`
renders it as plain text prepended to the next prompt. Plain text = any model
consumes it → **transportability is a property of the design, not a feature to
maintain.** Proven: the recall block is byte-identical regardless of which vendor
generated the turns (`cross-turn.test.ts`).

---

## What's proven (keep)

- **Standing directives** — `always/never/from now on/remember to/I told you to …`
  extract to durable `directive` facts, keyed by content (restating dedupes,
  distinct directives coexist). Recalled 19 turns later; vendor-portable. ✅
- **First-person self-facts** — `my name is Ada`, `my project is called Rotor` →
  the session user's slots, retrievable by the closed op. ✅
- **Event history + result cache + fact store** — deterministic, durable (SQLite),
  supersession-correct, space-isolated, property-tested since Phase 2. ✅
- **Semantic recall** — deterministic hashed-ngram cosine over turns. Reliable for
  **keyword / identifier overlap** (e.g. recalling the `parseConfig` turn from a
  `config parser parseConfig` query). ✅ *with the bound below.*

## HDC associative grounding — the capacity answer

An entity's cortex is a **bundle** of its `role⊗filler` binds; superposition noise
grows with the number of pairs, so recall is perfect only while `vector_dim` is
large enough. We swept it (`hdc-capacity.test.ts`, seed 42):

| `vector_dim` | 100% recall up to | graceful decay |
| --- | --- | --- |
| 1 000 | ~32 facts/entity | 92% @ 50, 66% @ 80 |
| 2 000 | ~50 | 94% @ 80 |
| 4 000 | ~80 | 99% @ 100 |
| **10 000 (default)** | **~128** | 99% @ 200, 94% @ 320, 71% @ 500 |
| 32 000 | ~500 | 97% @ 800 |

**Rule of thumb: perfect recall while `facts_per_entity ≲ vector_dim / 75`.**

**Verdict: keep, with bounds.** At the default dim, grounding is reliable to ~128
distinct role-fillers *per entity* — real entities rarely exceed that, and
`cascade` (§7.18) consolidates old facts out of the hot set. Two safety properties
make overload safe rather than dangerous:

1. Degradation is **graceful** (well above chance), not catastrophic.
2. The hard gate refuses on **low margin** (Phase 6), so an overloaded cortex
   *refuses* rather than confidently returning the wrong filler.

Auto-dimensioning (raise `vector_dim` when an entity's fact count approaches the
envelope) is a clean future improvement, not a correctness fix.

## What's bounded (documented, not cut)

- **Semantic recall quality** — the deterministic embedding is strong on
  lexical/subword overlap, **weak on purely conceptual** similarity (a query with no
  shared tokens won't surface a related turn). This is the basic tier; a neural
  embedding is the premium lane, and §7.7 already requires it to be **checkpointed**
  at the embedding boundary to stay replay-safe.
- **NL fact extraction** — the absorb enricher is deterministic pattern matching
  (directives, `my X is Y`, `X's R is Z`, `X lives in Y`, `X has Y`). It does **not**
  do coreference or compound sentences ("my name is Ada *and* my project is Rotor"
  splits on sentence boundaries, not clauses). Full extraction is the model
  enricher (premium). Nothing cut — the deterministic layer is the reliable floor.

## Nothing was cut

Every layer earns its place: durable directives/facts and HDC grounding are
reliable within documented, tested bounds; semantic recall is a useful basic tier
with a clear premium upgrade path. The vendor-portability guarantee holds because
the memory layer is, by construction, independent of the model.
