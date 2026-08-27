# The dict lane — a secondary vector over every inbound word

**Status:** design draft (2026-08-26), not built. Companion to the fact
substrate (`src/facts/`) and the loose-capture change; supersedes the
prime-overlap selection in `renderFactBlock` when built. The canonical glyph
cortex and its `space_id` are untouched — this is a SECOND, derived lane.

---

## The problem it solves

Selection for the per-turn fact block runs on `decompose()` — a closed NSM
prime vocabulary. Domain words vanish:

```
"always use abc for forms"                → ["A_LONG_TIME", "DO"]
"build me a signup form with validation"  → ["DO", "I"]
```

The only overlap between a standing rule and the exchange it governs is `DO` —
shared with every task in the ledger. The word that carries the association
("form") is invisible on both sides. At ledger scale the right fact ranks
below the block's 12 slots and the rule silently fails to fire.

Fact-side lexical anchors are not the fix: per-fact surface forms are exemplar
creep — stemming, synonyms, regex — smeared across ten thousand rows.

## The shape: three vocabularies, one algebra, literals as payload only

The substrate already mints a deterministic atom for ANY string on demand
(`generateSymbol(seed, key, dim)` — sha256 → MT19937, oracle-verified, zero
storage). The vocabulary was never closed at the HDC layer; only at
`decompose()`. The dict lane opens it, with discipline:

| tier | vocabulary | owns | cardinality |
| --- | --- | --- | --- |
| **atoms** | every inbound word | exact anchoring | unbounded, seeded on demand |
| **primes** | NSM (65) | universal semantics, value normalization | closed |
| **taxonomy labels** | activity/domain concepts + curated glosses | synonymy/paraphrase | ~40–60, org-extensible with audit |
| **literals** | `abc`, `7311`, names | payload — rendered, cited, **never matched** | — |

Atoms are quasi-orthogonal: `atom("form")` ⟂ `atom("signup")`. The dict lane
therefore buys exact domain anchoring with clean algebra and buys ZERO
paraphrase — that is the taxonomy glosses' job, and the two compose.

## The org lexicon (the "dict")

A per-org table, additive-only, statistics not authority:

```
lexicon(word text pk, count bigint, first_seen, last_seen)
```

Every inbound word — user turns, assistant turns, fact values (schema slots
AND preserved extras), rule glosses — normalized (lowercase, unicode-fold) and
counted. Powers three things:

- **IDF weights**: `idf(w) = log(1 + N/count(w))` — "abc" (count 1) is worth a
  ton; "do"/"the" worth ~nothing. The cure for common-atom dominance, same
  disease as `DO`-dominance in prime overlap.
- **Novelty signal**: a never-seen word in an exchange is high-information —
  a write-side salience hint (turns dense in novel terms likely deserve a
  fact) and a read-side boost for facts born in that exchange.
- **Epochs**: IDF drifts as the lexicon grows, which would break replay.
  Selection is deterministic GIVEN (ledger, lexicon epoch); epochs advance
  explicitly (e.g. nightly), the epoch id rides the run log, replays pin it.

## The vectors

**Per fact — the dict vector** (derived beside the cortex, rebuilt on
hydration from concept JSON + current epoch; no migration ever):

```
v_fact = L2norm(  Σ_w  idf(w)·atom(w)      w ∈ values(clean) ∪ values(extra) ∪ name
                ⊕ Σ_p  α_p·atom(p)          p ∈ primes stamped at write
                ⊕ Σ_l  α_l·atom(l)          l ∈ applies_to labels (directives) )
```

Float accumulation, not bipolar majority — weights must survive. Suggested
dim 2048 (separate atom family from the 10k glyph space; ~8 KB/fact float32 →
10k facts ≈ 80 MB in-memory index). If cross-probing against glyph space is
wanted later, mint at 10k and pay the memory.

**Per turn — the exchange vector**: same construction over the exchange tail —
recency-weighted (latest user turn ×2), IDF-damped, capped (~300 words),
⊕ its primes ⊕ its classified labels.

**Taxonomy glosses are dict vectors too** — classification collapses into the
same machinery: exchange labels = top-k cosine(v_exchange, v_gloss), generous
k, permissive gate. One curated place (the gloss) owns all surface language.

## Selection (renderFactBlock v2) — fixed shape preserved

- **Directive slots (≤4)** — rules (`kind: directive`, action ∈ closed set,
  object literal, `applies_to` labels, optional condition). Force-include
  when `applies_to ∩ exchange labels ≠ ∅` or trigger cosine clears a
  permissive gate; trigger-less directives always ride, confidence-ranked.
  Asymmetry rule: a directive firing needlessly costs one slot; failing to
  fire costs correctness — tune permissive.
- **Fact slots (≤8)** — ranked by `cos(v_exchange, v_fact)` with confidence
  and recency adjustments. Replaces prime-overlap scoring wholesale; primes
  still contribute INSIDE the vectors.

Everything on the read path is deterministic and model-free: same exchange,
same epoch → same block, sub-millisecond.

## Additive rules folded in (the 2026-08-26 decisions)

- **Loose capture** feeds the lane: preserved off-schema slots are first-class
  dict-vector content — the codename a model put in `relational.has_codename`
  is exactly as selectable as a schema slot.
- **Write-side directive sweep**: imperative standing-rule phrasing in user
  turns ("always/never/from now on/use X for Y") deterministically forces a
  distill when the model doesn't volunteer one — the one fact class where
  silent loss is unforgivable stops being optional.
- **Honest reporting** unchanged: `preservedSlots` in tool results.

## Rings

Outer ring (frontier APIs): the dict lane is entirely context-side — it makes
the injected block reliable, which is all an API model can receive. Inner
ring (William/llama.cpp): the SAME selected facts seed deeper hooks — logit-
bias candidates from selected facts' literals, and the grounding gate
(probe/verify during decode). One substrate, both rings.

## Failure modes, named

- **Bundle saturation**: majority/summation capacity is real — cap tokens per
  fact and per exchange; trigram atoms deferred until the needle test proves
  the need (morphology help, capacity cost).
- **IDF drift vs replay**: solved by epochs (above), or it silently breaks
  the determinism story.
- **Multilingual**: atoms are per surface form; cross-language synonymy lives
  in glosses only. Acceptable; do not fix at the atom tier.
- **Taxonomy gardening**: the honest recurring cost. One ~60-row file beats
  per-fact exemplars; extension is a rare, auditable ledger event.

## The test that gates it

Needle: plant the abc-forms rule; run 10 form-building tasks — 5 sharing the
literal word, 5 pure paraphrase — plus 10 unrelated tasks. Score block
appearances. Atoms must nail the literal 5 with 0 unrelated leaks; glosses
earn their keep on the paraphrase 5. "Too loose" has a number: the right
fact ranking > 12th.
