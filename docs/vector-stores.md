# Pluggable vector stores & spec-defined dimensions

Letting a deployment pick its vector store, and keeping the two distinct vectors
rrotor uses within each backend's real limits. It exists because "pgvector maxes
out at 2000 dims" is a real constraint — but it lands on only *one* of our vectors,
and only for the *index*.

**Status:** the Postgres + pgvector backend is **built** —
`src/exec/pgvector-store.ts`, selected by `ROTOR_STATOR_BACKEND=pgvector`, tested
against both in-process PGlite and a real Postgres (`test/memory/pgvector.test.ts`,
`pgvector-real.test.ts`). Facts (with `tier`/`session`), the sessions ordinal table,
turns (with an `hnsw`-indexed embedding), the event history, result cache, and kv are
all durable, and reads/writes are **live** (multiple pods sharing one database see
each other's writes mid-run). HDC-cortex persistence remains the one deferred piece
(see below).

## Two vectors, two very different needs

rrotor stores two unrelated kinds of vectors. Conflating them is the trap.

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

## The seam

The stator is an interface — `Stator` in `src/exec/store.ts` — and a vector store is
**not a new seam**, just a new backend behind it. Nothing above the stator changes.
Selection lives in `src/exec/stator.ts`:

```
ROTOR_STATOR_BACKEND = memory | sqlite | pgvector   // + others later (qdrant, …)
ROTOR_STATOR_URL     = connection string / file path
ROTOR_EMBED_DIM      = turn-embedding dimension (default 256)
```

`createStator()` builds the **synchronous** backends (`memory`, `sqlite`).
`pgvector` is async — it connects and creates its schema before serving — so it is
built by `initStator()` / `statorFromEnvAsync()`, which the server entrypoint awaits.
`createStator({ backend: "pgvector" })` throws rather than silently degrading a
durable backend to in-process.

## Concurrency: live reads (mirror dropped)

The `Stator` interface is now **async** (E6), so `PgVectorStore` reads and writes
**live** against Postgres — there is no in-memory mirror:

- Every fact query fetches the current rows and runs the SAME pure closed-op engine
  (`facts.ts`) the other backends use, so behaviour is byte-identical.
- Every write (`writeFacts`/`addTurn`/`touchSession`/`kvSet`/`history.append`/
  `cache.put`) is awaited straight to Postgres; a persistence error surfaces at the
  write call rather than being deferred. `flush()` is a no-op kept for symmetry.
- `touchSession` assigns its ordinal atomically at insert time
  (`ON CONFLICT (id) DO NOTHING`), so a pod that loses the race reads the winner's
  ordinal.

**Multi-pod, tested:** two live stores over one database see each other's committed
writes mid-run — no restart (`test/memory/pgvector.test.ts`, "live cross-pod
visibility"). **Determinism is preserved** because it never depended on *where* a
read came from: golden replay returns recorded outputs from the tape (§5.4) and never
re-reads the stator, so live reads on fresh execution don't perturb it.

### What the `pgvector` backend implements

The full `Stator` surface the SQLite store satisfies, plus honest dim-cap handling —
all live over Postgres (`test/memory/pgvector.test.ts`):

- `writeFacts` / `snapshotFacts` / `query` / `lookupFact` / `fillers` — facts, with
  `tier`/`session` columns and the `sessions` ordinal table (parity with SQLite; each
  read fetches the current rows and runs the same pure engine, so supersession/`prev`
  behave identically).
- `turns` / `addTurn` — the embedding corpus, in an `hnsw`-indexed `vector` column.
  `semanticRecallDb()` is the large-corpus ANN path (`embedding <=> query`); the
  short-term-recall path (§7.7) reads turns live and runs the pure cosine.
- `history` / `cache` / `kv` — the append-only StepRecord log, result cache, and kv
  scratch, all read/written live.
- **Dim validation at construction:** if the embedding dim exceeds the `hnsw`/
  `ivfflat` cap (2000), the `vector` column is created **without** an index and ANN
  falls back to an exact scan (`vectorIndexed === false`). Never truncate; never
  crash.

### Spec-defined dimensions (already wired for HDC)

`spec.space.vector_dim` already flows `executor.ts` → `grounding.computeSpaceId` →
`hdcSpace`, and it is part of `space_id = sha256(vector_dim, encoder_seed,
roles_config)` so two runs can't mix incompatible spaces. A rotor already picks its
HDC dimension. The remaining work is to let the **embedding** dim be spec/config
driven the same way, and to have each backend advertise its caps so `space_id`
negotiation can refuse an unstorable combination up front.

## Testing without a server

The backend is tested at two fidelities. Fast unit tests run against **PGlite** — an
in-process WASM Postgres with the pgvector extension (`@electric-sql/pglite`, a dev
dep). The same `PgLike` client interface is satisfied by both PGlite (tests) and
`pg`'s `Pool` (real deployments, lazy-imported so `pg` is never pulled into
memory/sqlite/injected setups). So the ANN queries, `hnsw` index, hydrate, and
error-latch paths are all exercised in `npm run verify`.

For real-server fidelity, `scripts/pg-setup.sh` provisions a local Postgres +
pgvector cluster and prints a `ROTOR_TEST_PG_URL`; `test/memory/pgvector-real.test.ts`
then drives the store over the actual `pg` driver + a real `hnsw` index (it
`skipIf(!ROTOR_TEST_PG_URL)`, so a bare dev box just skips it). CI runs a
`pgvector/pgvector:pg16` service container and sets the URL, so every push exercises
the backend on real Postgres.

## Deferred / open questions

- **HDC-cortex persistence** — the one Stator piece not yet on Postgres. The cortex
  is derivable by re-encoding facts, so it's rebuildable rather than lost; storing it
  directly (`bytea` bit-packed, since bipolar is 1 bit/dim and it's never indexed) is
  a follow-up.
- **Embedding dim from the spec** — HDC `vector_dim` is spec-driven; the *embedding*
  dim is currently `ROTOR_EMBED_DIM` / a `create()` option. Threading it through
  `spec.space` and advertising each backend's caps for `space_id` negotiation is the
  next refinement.
- **Which stores beyond pgvector** — Qdrant/Weaviate each become a `Stator` backend
  behind the same seam. Pick by what glyphh deployments actually run.
- **Multi-pod concurrent writers** — done (E6 async interface + E7 live reads): pods
  sharing one database see each other's committed writes mid-run. A remaining
  refinement is wrapping each `writeFacts` supersession+insert in a transaction for
  strict atomicity under heavy concurrent contention (today it is two awaited
  statements; fine for single-writer and light contention).
