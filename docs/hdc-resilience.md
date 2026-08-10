# HDC resilience — associative memory as a third recall lane

**Status:** design doc. The primitives (`HdcSpace`, `BasicGrounding` — bind / bundle /
cleanup / probe / verify) are **shipped** (`src/exec/hdc.ts`, `src/plugins/grounding.ts`);
schema-on-write (the enricher) and neural embeddings are **shipped and composed** in
recall (`recallContext`, `src/exec/recall.ts`). The **fusion of HDC into recall** —
the multi-facet record encoding and the three-lane recall described here — is **not yet
wired**. This doc is the target.

---

## The idea in one line

Every fact is **triple-encoded on write** — structural (the fact table), **HDC**
(an associative, deterministic, compositional record), and neural (fuzzy semantic) —
and recall **fuses all three**. HDC is the lane that gives *associative resilience*:
the whole record is reconstructable from any partial cue, and it degrades gracefully
under noise instead of failing.

## Why a third lane (what HDC has that the others don't)

| | HDC (NSM-prime) | Neural embeddings | Fact table |
| --- | --- | --- | --- |
| Nature | deterministic, **replay-safe**, compositional | opaque, fuzzy, needs checkpointing to replay | exact |
| Recall | **content-addressable** — probe any facet → the bound remainder, with a margin | nearest-vector similarity | exact key only |
| Failure mode | **graceful degradation** (holographic) | silent wrong-neighbour | brittle (wrong key → nothing) |
| Extra skill | **grounding / abstention** (refuse on low margin), reinforcement (same fact ×N → stronger) | — | supersession |

The three are **complementary, not redundant**: HDC gives structure + resilience,
neural fills the soft associations HDC's discrete binds miss, the table gives exact
current values. HDC is also the **determinism moat** — a replay-safe semantic memory
no vector/graph store can claim.

## The NSM-prime basis

The role vocabulary is Glyph's HDC version of the **NSM semantic primes** — the ~65
universal semantic primitives (KNOW, DO, HAPPEN, THIS, SOMEONE, GOOD, PLACE, TIME, …)
that every concept decomposes into. Using primes as the role basis makes the space a
**universal compositional substrate**: a user fact, a relation, a temporal event, a
**code path** all encode into the *same* space, addressable by the same primitive roles.

> Open-tier reality: `HdcSpace.symbol()` currently maps role/filler *strings* to
> deterministic bipolar hypervectors (a hashed basis). The NSM-prime basis is the
> **premium grounding engine** that swaps in behind `GroundingPlugin` — same interface,
> prime-grounded symbols. `computeSpaceId(dim, seed, "universal-7x33")` names the
> shared space.

## Multi-facet record encoding (the write side)

Today an entity's cortex bundles only `role⊗filler`. The resilience design binds
**multiple facets** of a fact into one record hypervector:

```
record = role⊗filler  ⊕  glyphhRole⊗g  ⊕  segment⊗s  ⊕  layer⊗l
```

- **role⊗filler** — the core assertion (the prime-grounded semantic slot).
- **layer / segment** — **already fully specified** by the canonical Glyph structure
  (below) — not an open question.

Because bind is self-inverse and bundle superposes, the record is **probeable by any
facet**: unbind by `segment` → everything in that segment; by `role` → the filler; by
`layer` → one abstraction level. That is multi-key associative retrieval — a partial
cue reconstructs the bound remainder.

### The canonical Glyph structure (already mapped + implemented)

The layer/segment/role ontology is defined and implemented in the Python runtime —
`glyphh-runtime/glyphh/memory/thought_glyph.py` (`THOUGHT_ENCODER_CONFIG`, Ada's
`ThoughtGlyphEncoder`) over the NSM primes in `runtime/glyphh_rotor/primes/primes.py`.
**dimension = 2000, seed = 42** (the "2k space"). Every thought becomes a Glyph with
**5 layers** (each segment holds a prime-grounded role; qualified key `{layer}_{segment}`):

| Layer (facet) | similarity wt | Segments |
| --- | --- | --- |
| **perspective** (WHO) | 0.25 | self, other, third, group |
| **semantic** (WHAT) | 0.30 | identity, quality, quantity, emotion, category |
| **relational** (HOW) | 0.25 | equals, possession, causation, comparison, action |
| **temporal** (WHEN) | 0.10 | past, present, future |
| **direction** (SOURCE) | 0.10 | incoming, outgoing |

Encode: text + speaker → match words to primitive roles (`PrimitiveSpace`, closed
prime vocabulary — the model may *select* primes, never invent) → route into the
activated layers/segments → **bind role→value, bundle into segments → layers → global
cortex**. Recall groups by the *query's own* primes — that grouping is the fact tree.
The per-layer `similarity_weight` is the weighted-recall knob.

This is the multi-facet record the resilience design calls for — so the TS stator's
job is to **adopt this structure**, not invent one.

### Space sizing — 2k dims + per-segment cortices

The resilience layer targets a **2 000-dim** space (smaller/cheaper than the 10k
default). Capacity is bounded: perfect recall while `fillers/entity ≲ dim/75`
(`hdc-capacity.test.ts`), so 2k ≈ ~25 fillers before superposition noise — and the
extra facets *multiply* binds per fact. Mitigation: **per-segment cortices** (one
bundle per segment) so each stays sparse, rather than one giant per-entity bundle.
Segment then does double duty — a facet key *and* the cortex partition.

## Three-lane recall (the read side)

For a query, fuse:

1. **Structural** — `memory.recall` current facts (exact, supersession-correct).
2. **HDC probe** — decompose the query to its prime roles/segments, `probe` the
   cortex → resilient structured recall **+ the margin gate**: surface a filler only
   if it wins with confidence; else *abstain*. This is the grounding / anti-hallucination
   lane and the LongMemEval `_abs` (abstention) mechanism.
3. **Neural** — fuzzy semantic top-K over facts + turns (`embedBatch` + cosine),
   for the deeper connections that share no HDC key.

Ranked/merged into the recall block (`recallContext`). Directives remain always-injected.

## Associative resilience — what it buys, concretely

- **Recall from partial / noisy cues** — a query that shares no words with the stored
  turn and isn't the exact key can still reconstruct the fact via the bound facets.
- **Graceful degradation** — an overloaded cortex *refuses on low margin* rather than
  confidently returning the wrong filler (safe failure).
- **Reinforcement** — a fact stated N times bundles N times → stronger recall.
- **Compositional / multi-hop** — traverse relations by bind/unbind; encode and walk
  code paths in the same space.
- **Deterministic** — replay-safe; the neural lane is checkpointed at the embedding
  boundary (§7.7) so the resilient core stays byte-for-byte reproducible.

## Determinism boundary

HDC and the fact table are pure/deterministic. The neural lane is **not** replay-safe
on its own; it must be **checkpointed at the embedding boundary** (§7.7 — record the
vectors, replay reads the tape, never re-invokes the model). Rule: the resilient,
grounding, and abstention behaviour is HDC (deterministic); neural only *augments*.

## Open questions

Resolved by the canonical structure above:
- ~~**Segment / Layer**~~ — **defined**: 5 layers × fixed segments (`thought_glyph.py`).
- ~~**Query → prime decomposition**~~ — **exists**: `primes.py` heuristic keyword→prime
  map (deterministic, always-on), sharpened by a local model constrained to the closed
  prime set. Recall groups by the query's primes.

Still open:
1. **TS port vs. call-through** — the glyph encoder + primes live in the **Python**
   runtime (`glyphh-runtime` / `runtime/glyphh_rotor`). The rrotor **TS** stator has only
   the basic `role⊗filler` HDC. Do we (a) port the 5-layer encoder + prime map to TS, or
   (b) have the TS stator call the Python runtime for glyph encoding/recall? Determines
   where the resilience lane physically runs.
2. **Fusion policy** — how the three lanes' candidates (exact / HDC probe+margin / neural)
   are merged and ranked into the recall block, and how the per-layer `similarity_weight`
   factors in.
3. **Determinism of the model-sharpened decomposition** — the heuristic prime map is
   pure; the model-sharpened variant must be checkpointed (§7.7) to stay replay-safe.

## Build plan (each step measured on the benchmark, not assumed)

1. **HDC probe as a recall lane + grounding/abstention gate.** Smallest step; the
   schema-on-write facts are already `(entity, role, filler)`, i.e. HDC-ready. No
   segment/layer needed. Testable now (LongMemEval `_abs` for the abstention column).
2. **Multi-facet encoding** — adopt the canonical 5-layer Glyph structure
   (`thought_glyph.py`, dim 2000/seed 42) with per-segment cortices. Structure is
   defined; the work is TS port vs. call-through (Open question 1).
3. **Neural fusion** (checkpointed) for the deeper connections.

## Where it lives

- Primitives: `src/exec/hdc.ts` (`HdcSpace`: bind/bundle/cleanup), `src/plugins/grounding.ts`
  (`BasicGrounding`: probe/verify/groundedFillers).
- Recall assembly: `src/exec/recall.ts` (`recallContext`) — where lanes 2 and 3 fuse.
- Write: `src/harness/stator-api.ts` (`statorWrite`) — where the multi-facet record
  is encoded alongside the fact write.
- Capacity envelope: `test/memory/hdc-capacity.test.ts`.
