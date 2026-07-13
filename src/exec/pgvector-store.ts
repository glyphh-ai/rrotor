/**
 * The Postgres + pgvector stator — the durable, shared-cross-pod backend behind
 * the same {@link Stator} interface as {@link InProcessStore} and
 * {@link SqliteStore} (docs/vector-stores.md; docs/runtime.md §2.5, §3.2; SPEC.md
 * §17.1). This is the premium stator the design doc pointed at.
 *
 * ## Why the "hydrate-then-flush mirror" shape
 *
 * The `Stator` interface is **synchronous** (better-sqlite3 is), but every
 * Postgres driver is async. Rather than make the whole control plane async, this
 * backend keeps a synchronous **in-memory mirror** — byte-identical to
 * {@link InProcessStore}, running the SAME pure fact engine (facts.ts) — as the
 * authoritative state *during a run*. So control-plane determinism is untouched:
 * reads and writes never await, and golden replay behaves exactly as on the other
 * backends.
 *
 * Postgres is the **durability + sharing** layer, strictly OFF the control path:
 *  - `create()` **hydrates** the mirror from Postgres (facts, sessions, turns,
 *    history, cache, kv) before the sync run loop starts.
 *  - Each mutation updates the mirror synchronously AND enqueues an async
 *    write-through to Postgres (serialized on a promise chain — a single PGlite
 *    connection requires it, and it preserves write order).
 *  - `flush()` is the durability barrier; `shutdown()` flushes then closes.
 *
 * Consistency model: single-tenant (one runtime = one user, docs/memory.md), so
 * the mirror is authoritative and Postgres is eventually-consistent across pods.
 * True multi-pod concurrent writers would need an async Stator interface — a
 * deliberately deferred, larger change.
 *
 * ## The two vectors (docs/vector-stores.md)
 *
 * Turn embeddings (the ANN-searched vector, default dim 256) live in an
 * `hnsw`-indexed `vector` column — this is the pluggable vector store. The HDC
 * hypervector (dim ~10k) is per-entity and never cross-row searched, so it is not
 * stored here (it is derivable by re-encoding facts); that persistence is a
 * scoped follow-up. The pgvector `hnsw`/`ivfflat` index caps at 2000 dims, so an
 * embedding dim above that **degrades to an unindexed exact scan** rather than
 * failing (the "degrade, never raise" rule).
 */

import { embed } from "./embedding.js";
import {
  applyFactWrite,
  currentFillers,
  lookupCurrentFact,
  runClosedOp,
  type Fact,
  type QueryResult,
  type Row,
} from "./facts.js";
import type { StepRecord } from "../types.js";
import type { CacheEntry, EventHistory, ResultCache, Stator } from "./store.js";

/** The minimal async client both `pg` (node-postgres `Pool`) and PGlite satisfy.
 *  Postgres placeholders are `$1,$2,…`; both drivers agree. */
export interface PgLike {
  query(sql: string, params?: unknown[]): Promise<{ rows: Array<Record<string, unknown>> }>;
  /** PGlite multi-statement exec; absent on `pg` (we fall back to `query`). */
  exec?(sql: string): Promise<unknown>;
  /** PGlite teardown. */
  close?(): Promise<void>;
  /** node-postgres pool teardown. */
  end?(): Promise<void>;
}

/** The pgvector index dimension ceiling (`hnsw`/`ivfflat`). Above it we skip the
 *  index and exact-scan. `halfvec` (4000) is a future refinement. */
const INDEX_DIM_CAP = 2000;

export interface PgVectorOptions {
  /** Inject a ready client (tests pass a PGlite instance). */
  client?: PgLike;
  /** Postgres connection string; lazy-loads `pg` when no client is injected. */
  url?: string;
  /** Turn-embedding dimension (must match the runtime's embedding, §7.7). */
  embedDim?: number;
  /** Optional table-name prefix for multi-space isolation on a shared DB. */
  schemaPrefix?: string;
}

function ddl(embedDim: number, indexable: boolean): string {
  return `
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS step_records (
  seq     BIGSERIAL PRIMARY KEY,
  run_id  TEXT NOT NULL,
  step_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  rec     JSONB NOT NULL,
  UNIQUE (run_id, step_id, attempt)
);
CREATE INDEX IF NOT EXISTS ix_step_records_run ON step_records(run_id, seq);
CREATE TABLE IF NOT EXISTS result_cache (
  key          TEXT PRIMARY KEY,
  output       JSONB NOT NULL,
  scope        TEXT NOT NULL,
  expires_tick BIGINT
);
CREATE TABLE IF NOT EXISTS facts (
  seq        BIGSERIAL PRIMARY KEY,
  entity     TEXT NOT NULL,
  role       TEXT NOT NULL,
  filler     TEXT NOT NULL,
  space_id   TEXT,
  fact_key   TEXT,
  is_current BOOLEAN NOT NULL,
  speaker    TEXT,
  tick       BIGINT NOT NULL,
  tier       TEXT,
  session    TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  id      TEXT PRIMARY KEY,
  ordinal INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
  seq       BIGSERIAL PRIMARY KEY,
  text      TEXT NOT NULL,
  embedding vector(${embedDim})
);
${indexable ? "CREATE INDEX IF NOT EXISTS ix_turns_embedding ON turns USING hnsw (embedding vector_cosine_ops);" : ""}
`;
}

/** Render a vector literal for a `$n::vector` bind. */
function vec(v: number[]): string {
  return "[" + v.join(",") + "]";
}

export class PgVectorStore implements Stator {
  // ── the synchronous in-memory mirror (authoritative during a run) ──────────
  private readonly recs: StepRecord[] = [];
  private readonly cacheMap = new Map<string, CacheEntry>();
  private readonly factRows: Fact[] = [];
  private readonly kvMap = new Map<string, unknown>();
  private readonly turnLog: string[] = [];
  private readonly sessionMap = new Map<string, number>();
  private sessionCounter = 0;

  // ── the async durability layer ─────────────────────────────────────────────
  private readonly db: PgLike;
  private readonly embedDim: number;
  private readonly indexable: boolean;
  private flushChain: Promise<void> = Promise.resolve();
  private lastError: unknown;
  private closed = false;

  private constructor(db: PgLike, embedDim: number) {
    this.db = db;
    this.embedDim = embedDim;
    this.indexable = embedDim <= INDEX_DIM_CAP;
  }

  /** Connect (or adopt an injected client), create the schema, and hydrate the
   *  mirror. The only async entry point — call it before the sync run loop. */
  static async create(opts: PgVectorOptions = {}): Promise<PgVectorStore> {
    const embedDim = opts.embedDim ?? 256;
    const db = opts.client ?? (await connectPg(opts.url));
    const store = new PgVectorStore(db, embedDim);
    await store.execScript(ddl(embedDim, store.indexable));
    await store.hydrate();
    return store;
  }

  private async execScript(sql: string): Promise<void> {
    if (this.db.exec) await this.db.exec(sql);
    else await this.db.query(sql); // node-postgres runs multi-statement simple queries
  }

  /** Rebuild the mirror from Postgres so a restarted pod resumes exactly (§17.1). */
  private async hydrate(): Promise<void> {
    const facts = (await this.db.query(
      "SELECT entity, role, filler, space_id, fact_key, is_current, speaker, tick, tier, session FROM facts ORDER BY seq ASC",
    )).rows;
    for (const r of facts) {
      const f: Fact = {
        entity: String(r.entity),
        role: String(r.role),
        filler: String(r.filler),
        is_current: r.is_current === true || r.is_current === 1,
        tick: Number(r.tick),
      };
      if (r.space_id != null) f.space_id = String(r.space_id);
      if (r.fact_key != null) f.key = String(r.fact_key);
      if (r.speaker != null) f.speaker = String(r.speaker);
      if (r.tier != null) f.tier = String(r.tier) as Fact["tier"];
      if (r.session != null) f.session = String(r.session);
      this.factRows.push(f);
    }
    const sessions = (await this.db.query("SELECT id, ordinal FROM sessions")).rows;
    for (const r of sessions) this.sessionMap.set(String(r.id), Number(r.ordinal));
    this.sessionCounter = sessions.length === 0 ? 0 : Math.max(...sessions.map((r) => Number(r.ordinal))) + 1;

    const turns = (await this.db.query("SELECT text FROM turns ORDER BY seq ASC")).rows;
    for (const r of turns) this.turnLog.push(String(r.text));

    const recs = (await this.db.query("SELECT rec FROM step_records ORDER BY seq ASC")).rows;
    for (const r of recs) this.recs.push(asJson(r.rec) as StepRecord);

    const cache = (await this.db.query("SELECT key, output, scope, expires_tick FROM result_cache")).rows;
    for (const r of cache) {
      this.cacheMap.set(String(r.key), {
        output: asJson(r.output) as Row,
        scope: String(r.scope),
        expiresTick: r.expires_tick == null ? undefined : Number(r.expires_tick),
      });
    }
    const kv = (await this.db.query("SELECT k, v FROM kv")).rows;
    for (const r of kv) this.kvMap.set(String(r.k), asJson(r.v));
  }

  /** Enqueue a write-through, serialized and error-latched — never throws into
   *  the synchronous caller (durability is off the control path). */
  private enqueue(fn: () => Promise<void>): void {
    this.flushChain = this.flushChain
      .then(() => (this.closed ? undefined : fn()))
      .catch((e) => {
        this.lastError = e;
      });
  }

  /** Durability barrier: await all pending write-throughs; re-raise the first
   *  persistence error (so callers/tests learn about a broken sink). */
  async flush(): Promise<void> {
    await this.flushChain;
    if (this.lastError !== undefined) {
      const e = this.lastError;
      this.lastError = undefined;
      throw e;
    }
  }

  /** Flush, then release the client. Awaitable clean shutdown. */
  async shutdown(): Promise<void> {
    await this.flush();
    this.closed = true;
    if (this.db.close) await this.db.close();
    else if (this.db.end) await this.db.end();
  }

  // ── event history (§5.4) ───────────────────────────────────────────────────
  readonly history: EventHistory = {
    append: (rec) => {
      this.recs.push(rec);
      this.enqueue(async () => {
        await this.db.query(
          "INSERT INTO step_records (run_id, step_id, attempt, rec) VALUES ($1,$2,$3,$4) ON CONFLICT (run_id, step_id, attempt) DO NOTHING",
          [rec.run_id, rec.step_id, rec.attempt, JSON.stringify(rec)],
        );
      });
    },
    read: (runId) => this.recs.filter((r) => r.run_id === runId),
    lookup: (runId, stepId, attempt) =>
      this.recs.find((r) => r.run_id === runId && r.step_id === stepId && r.attempt === attempt),
    lastAttempt: (runId, stepId) => {
      let max = -1;
      for (const r of this.recs) {
        if (r.run_id === runId && r.step_id === stepId && r.attempt > max) max = r.attempt;
      }
      return max;
    },
  };

  // ── result cache (§5.7) ─────────────────────────────────────────────────────
  readonly cache: ResultCache = {
    get: (key, tick) => {
      const e = this.cacheMap.get(key);
      if (!e) return undefined;
      if (e.expiresTick !== undefined && tick >= e.expiresTick) return undefined;
      return e.output;
    },
    put: (key, output, opts) => {
      const entry: CacheEntry = { output, scope: opts.scope ?? "rotor", expiresTick: opts.ttlTicks };
      this.cacheMap.set(key, entry);
      this.enqueue(async () => {
        await this.db.query(
          "INSERT INTO result_cache (key, output, scope, expires_tick) VALUES ($1,$2,$3,$4) " +
            "ON CONFLICT (key) DO UPDATE SET output=excluded.output, scope=excluded.scope, expires_tick=excluded.expires_tick",
          [key, JSON.stringify(output), entry.scope, entry.expiresTick ?? null],
        );
      });
    },
  };

  // ── facts — mirror runs the shared pure engine; write-through mirrors SQLite ─
  writeFacts(facts: Fact[]): number {
    const n = applyFactWrite(this.factRows, facts);
    for (const f of facts) {
      this.enqueue(async () => {
        if (f.key) {
          await this.db.query("UPDATE facts SET is_current = FALSE WHERE fact_key = $1 AND is_current = TRUE", [f.key]);
        }
        await this.db.query(
          "INSERT INTO facts (entity, role, filler, space_id, fact_key, is_current, speaker, tick, tier, session) " +
            "VALUES ($1,$2,$3,$4,$5,TRUE,$6,$7,$8,$9)",
          [f.entity, f.role, f.filler, f.space_id ?? null, f.key ?? null, f.speaker ?? null, f.tick, f.tier ?? null, f.session ?? null],
        );
      });
    }
    return n;
  }

  lookupFact(entity: string, role: string, spaceId?: string): Fact | undefined {
    return lookupCurrentFact(this.factRows, entity, role, spaceId);
  }
  fillers(entity: string, role: string, spaceId?: string): string[] {
    return currentFillers(this.factRows, entity, role, spaceId);
  }
  query(op: string, params: Row, spaceId?: string): QueryResult {
    return runClosedOp(op, params, this.factRows, spaceId);
  }
  snapshotFacts(): Fact[] {
    return this.factRows.slice();
  }

  // ── kv ──────────────────────────────────────────────────────────────────────
  kvGet(key: string): unknown {
    return this.kvMap.get(key);
  }
  kvSet(key: string, value: unknown): void {
    this.kvMap.set(key, value);
    this.enqueue(async () => {
      await this.db.query(
        "INSERT INTO kv (k, v) VALUES ($1,$2) ON CONFLICT (k) DO UPDATE SET v = excluded.v",
        [key, JSON.stringify(value ?? null)],
      );
    });
  }

  // ── turns — the embedding corpus, stored with its ANN vector ────────────────
  addTurn(text: string): void {
    this.turnLog.push(text);
    const v = vec(embed(text, this.embedDim));
    this.enqueue(async () => {
      await this.db.query("INSERT INTO turns (text, embedding) VALUES ($1, $2::vector)", [text, v]);
    });
  }
  turns(): string[] {
    return this.turnLog.slice();
  }

  // ── sessions ──────────────────────────────────────────────────────────────
  touchSession(id: string): number {
    let o = this.sessionMap.get(id);
    if (o === undefined) {
      o = this.sessionCounter++;
      this.sessionMap.set(id, o);
      const ord = o;
      this.enqueue(async () => {
        await this.db.query("INSERT INTO sessions (id, ordinal) VALUES ($1,$2) ON CONFLICT (id) DO NOTHING", [id, ord]);
      });
    }
    return o;
  }
  sessionOrdinal(id?: string): number {
    if (id === undefined) return Math.max(0, this.sessionCounter - 1);
    return this.sessionMap.get(id) ?? 0;
  }

  /** Whether turn embeddings are `hnsw`-indexed (false ⇒ exact-scan fallback). */
  get vectorIndexed(): boolean {
    return this.indexable;
  }

  /**
   * ANN recall **in the database** — the large-corpus path that doesn't require
   * hydrating every turn into memory. Async (off the deterministic control
   * plane); the in-run recall still uses the pure in-memory cosine (§7.7). Returns
   * turns by descending cosine similarity. Uses the `hnsw` index when present,
   * else an exact scan (same SQL, planner picks).
   */
  async semanticRecallDb(query: string, topK = 8, threshold = 0.0): Promise<Array<{ text: string; score: number }>> {
    const q = vec(embed(query, this.embedDim));
    const rows = (await this.db.query(
      "SELECT text, 1 - (embedding <=> $1::vector) AS score FROM turns " +
        "WHERE embedding IS NOT NULL ORDER BY embedding <=> $1::vector LIMIT $2",
      [q, topK],
    )).rows;
    return rows
      .map((r) => ({ text: String(r.text), score: Number(r.score) }))
      .filter((h) => h.score >= threshold);
  }

  close(): void {
    // Sync interface conformance — schedule an unawaited teardown. Prefer
    // `await shutdown()` for a clean flush (the server does).
    void this.shutdown();
  }
}

/** Lazy-load node-postgres only when a real connection is needed, so `pg` is not
 *  pulled into environments that use the in-process/SQLite/injected backends. */
async function connectPg(url?: string): Promise<PgLike> {
  const mod: unknown = await import("pg");
  const Pool = (mod as { default?: { Pool: new (c: unknown) => unknown }; Pool?: new (c: unknown) => unknown }).Pool
    ?? (mod as { default: { Pool: new (c: unknown) => unknown } }).default.Pool;
  const pool = new Pool({ connectionString: url }) as {
    query(sql: string, params?: unknown[]): Promise<{ rows: Array<Record<string, unknown>> }>;
    end(): Promise<void>;
  };
  return {
    query: (sql, params) => pool.query(sql, params),
    end: () => pool.end(),
  };
}

/** JSONB comes back parsed from `pg`; PGlite may return a JSON string. */
function asJson(v: unknown): unknown {
  return typeof v === "string" ? JSON.parse(v) : v;
}
