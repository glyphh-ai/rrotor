# Pluggable vector stores & spec-defined dimensions

This is a **design doc** (not yet fully built) for letting a deployment pick its
vector store, and for keeping the two distinct vectors OpenRotor uses within each
backend's real limits. It exists because "pgvector maxes out at 2000 dims" is a real
constraint — but it lands on only *one* of our vectors, and only for the *index*.

## Two vectors, two very different needs

OpenRotor stores two unrelated kinds of vectors. Conflating them is the trap.

| | **HDC hypervector** | **Semantic-recall embedding** |
| --- | --- | --- |
| Source | `src/exec/hdc.ts` — bipolar `role⊗filler` binds, bundled per entity | `src/exec/embedding.ts` — hashed-ngram unit vector of a turn |
| Default dim | **10 000** (`spec.space.vector_dim`, bounded [64, 65536]) | 256 |
| Access pattern | probed by cosine **within one entity's cortex** | ANN search **across all turns** |
| Needs an ANN index? | **No** — never searched across rows | **Yes** — this is what a vector index accelerates |
| Where it lives | one column/blob per entity cortex | one indexed column per turn |

The consequence: **the 2000-dim ceiling is an *index* limit, and we only index the
embedding (256), which is comfortably under it.** The 10k HDC vector is *stored*, not
indexed, so it isn't subject to the index cap at all.

## The pgvector limits, precisely

- `vector` **stores** up to **16 000** dims.
- `hnsw` / `ivfflat` **indexes** cap at **2 000** dims (`halfvec` indexes at 4 000).

So on Postgres+pgvector:

- **HDC cortex (10k):** store as `vector(10000)` or `bytea` (bipolar packs to bits),
  **no index** — recall is an intra-entity scan of a handful of rows, not an ANN
  query. Fits today.
- **Embedding (256):** `vector(256)` with an `hnsw` index. Fits with room to spare.
- If a deployment raises the *embedding* dim past 2000 (a premium neural embedder),
  it must choose `halfvec` (≤4000) or accept an **unindexed exact scan** — the
  backend validates this and **degrades to a scan rather than failing** (the
  "degrade, never raise" rule).

## The seam (already present)

The stator is already an interface — `Stator` in `src/exec/store.ts`, constructed by
`createStator` / `statorFromEnv` in `src/exec/stator.ts`, selected by
`ROTOR_STATOR_BACKEND` (`memory` | `sqlite`, today). A vector store is **not a new
seam** — it's a new `Stator` backend behind the existing one. Nothing above the
stator changes.

```
ROTOR_STATOR_BACKEND = memory | sqlite | pgvector   // + others later (qdrant, …)
ROTOR_STATOR_URL     = connection string / file path
```

### What a `pgvector` backend implements

The same `Stator` surface the SQLite store already satisfies — plus honest handling
of the dim caps:

- `writeFacts` / `snapshotFacts` / `query` / `lookupFact` / `fillers` — facts, with
  `tier`/`session` columns and the `sessions` ordinal table (parity with SQLite).
- `turns` / `addTurn` — the embedding corpus, in an `hnsw`-indexed `vector` column.
- `history` / `cache` — the append-only StepRecord log and result cache.
- **Dim validation at construction:** read the space's `vector_dim` and the
  embedding dim; if either exceeds the chosen column/index's cap, log it and pick the
  index-free path for that column. Never silently truncate; never crash.

### Spec-defined dimensions (already wired for HDC)

`spec.space.vector_dim` already flows `executor.ts` → `grounding.computeSpaceId` →
`hdcSpace`, and it is part of `space_id = sha256(vector_dim, encoder_seed,
roles_config)` so two runs can't mix incompatible spaces. A rotor already picks its
HDC dimension. The remaining work is to let the **embedding** dim be spec/config
driven the same way, and to have each backend advertise its caps so `space_id`
negotiation can refuse an unstorable combination up front.

## Why this is a doc, not code yet

Standing up Postgres+pgvector touches deployment (a real DB, migrations, a connection
pool) and is hard to reverse. The seam is ready and the constraints are now written
down; building the backend is a scoped follow-up (candidate: the "durable cloud
stator" line in `BUILD_PLAN.md`). Until then SQLite is the durable local/single-node
store and in-process is the bare-box default.

## Open questions

- **HDC storage form** — `vector(10000)` (readable, larger) vs bit-packed `bytea`
  (compact, needs unpack). Lean `bytea`: bipolar is 1 bit/dim, and we never index it.
- **Which stores beyond pgvector** — Qdrant/Weaviate/pgvector cover most asks; each is
  a `Stator` backend. Pick by what glyphh deployments actually run.
- **Embedding-dim negotiation** — should an over-cap embedding dim hard-refuse at
  `space_id` time, or silently take the unindexed path? Current lean: refuse loudly
  in strict mode, degrade in bare-box mode.
