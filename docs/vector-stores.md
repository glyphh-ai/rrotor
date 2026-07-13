# Pluggable vector stores & spec-defined dimensions

Letting a deployment pick its vector store, and keeping the two distinct vectors
OpenRotor uses within each backend's real limits. It exists because "pgvector maxes
out at 2000 dims" is a real constraint — but it lands on only *one* of our vectors,
and only for the *index*.

**Status:** the Postgres + pgvector backend is **built** —
`src/exec/pgvector-store.ts`, selected by `ROTOR_STATOR_BACKEND=pgvector`, tested
against an in-process PGlite instance (`test/memory/pgvector.test.ts`). Facts (with
`tier`/`session`), the sessions ordinal table, turns (with an `hnsw`-indexed
embedding), the event history, result cache, and kv are all durable and hydrate on
restart. HDC-cortex persistence remains the one deferred piece (see below).

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
`pgvector` is async — it connects and hydrates before serving — so it is built by
`initStator()` / `statorFromEnvAsync()`, which the server entrypoint awaits.
`createStator({ backend: "pgvector" })` throws rather than silently degrading a
durable backend to in-process.

## Concurrency: the hydrate-then-flush mirror

The `Stator` interface is synchronous (better-sqlite3 is), but every Postgres driver
is async. Rather than make the whole control plane async, the pgvector backend keeps
a synchronous **in-memory mirror** — byte-identical to `InProcessStore`, running the
SAME pure fact engine (`facts.ts`) — as the authoritative state *during a run*:

- `PgVectorStore.create()` **hydrates** the mirror from Postgres (facts, sessions,
  turns, history, cache, kv) before the sync run loop starts.
- Each mutation updates the mirror synchronously **and** enqueues an async
  write-through to Postgres, serialized on a promise chain (a single PGlite
  connection requires it; it also preserves write order). Errors are latched, never
  thrown into the synchronous caller — durability is strictly off the control path.
- `flush()` is the durability barrier (awaits the chain, re-raises the first error);
  `shutdown()` flushes then releases the client.

**Determinism is preserved:** reads/writes never await, so golden replay behaves
exactly as on the other backends. Postgres is durability + cross-pod sharing only.
**Consistency:** single-tenant (one runtime = one user, docs/memory.md), so the
mirror is authoritative and Postgres is eventually-consistent across pods. True
multi-pod concurrent writers would need an async `Stator` interface — a deliberately
deferred, larger change.

### What the `pgvector` backend implements

The full `Stator` surface the SQLite store satisfies, plus honest dim-cap handling —
all durable and hydrated on restart (`test/memory/pgvector.test.ts`):

- `writeFacts` / `snapshotFacts` / `query` / `lookupFact` / `fillers` — facts, with
  `tier`/`session` columns and the `sessions` ordinal table (parity with SQLite; the
  same pure engine runs the closed ops, so supersession/`prev` behave identically).
- `turns` / `addTurn` — the embedding corpus, in an `hnsw`-indexed `vector` column.
  `semanticRecallDb()` is the async large-corpus ANN path (`embedding <=> query`);
  the in-run recall still uses the pure in-memory cosine (§7.7) so it stays
  deterministic.
- `history` / `cache` / `kv` — the append-only StepRecord log, result cache, and kv
  scratch, all persisted and rehydrated.
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
- **Multi-pod concurrent writers** — the `Stator` interface is now **async** (E6), so
  the prerequisite is in place. The remaining step is a pgvector *live-read mode* that
  reads facts/history from Postgres per query instead of from the hydrated mirror, so
  pods sharing one database see each other's writes mid-run. The mirror is
  single-tenant-authoritative until then.
