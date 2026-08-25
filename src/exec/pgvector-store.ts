/**
 * The Postgres + pgvector stator — the durable, shared-cross-pod backend behind
 * the same {@link Stator} interface as {@link InProcessStore} and
 * {@link SqliteStore} (docs/vector-stores.md; docs/runtime.md §2.5, §3.2; SPEC.md
 * §17.1). This is the premium stator the design doc pointed at.
 *
 * ## Live reads (multi-pod)
 *
 * Now that the `Stator` interface is async (BUILD_PLAN.md E6), this backend reads
 * and writes **live** against Postgres — there is no in-memory mirror. Every fact
 * query fetches the current rows and runs the SAME pure closed-op engine (facts.ts)
 * that the other backends use, so behaviour is byte-identical, and multiple pods
 * sharing one database see each other's committed writes mid-run.
 *
 * Determinism is preserved because it never depended on *where* a read came from:
 * golden replay returns recorded outputs from the tape (§5.4) and never re-reads the
 * stator, so live reads on fresh execution don't perturb it.
 *
 * The MEMORY-PLANE half (turn embeddings, ANN recall) was cut with the memory
 * plane (2026-08-24): this is now a plain-Postgres determinism store — events,
 * facts, kv, result cache — org-schema friendly. Historical note: it was not
 * stored here (it is derivable by re-encoding facts); that persistence is a scoped
 * "degrade, never raise" rule).
 */

import {
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

export interface PgVectorOptions {
  /** Inject a ready client (tests pass a PGlite instance). */
  client?: PgLike;
  /** Postgres connection string; lazy-loads `pg` when no client is injected. */
  url?: string;
}

function ddl(): string {
  return `
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
`;
}

/** A DB fact row → the shared {@link Fact} shape (matches SqliteStore's mapping). */
function rowToFact(r: Record<string, unknown>): Fact {
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
  return f;
}

export class PgVectorStore implements Stator {
  private readonly db: PgLike;

  private constructor(db: PgLike) {
    this.db = db;
  }

  /** Connect (or adopt an injected client) and create the schema. */
  static async create(opts: PgVectorOptions = {}): Promise<PgVectorStore> {
    const db = opts.client ?? (await connectPg(opts.url));
    const store = new PgVectorStore(db);
    await store.execScript(ddl());
    return store;
  }

  private async execScript(sql: string): Promise<void> {
    if (this.db.exec) await this.db.exec(sql);
    else await this.db.query(sql); // node-postgres runs multi-statement simple queries
  }

  /** All fact rows in write order — the input to the shared pure query engine. */
  private async allFacts(): Promise<Fact[]> {
    const rows = (await this.db.query(
      "SELECT entity, role, filler, space_id, fact_key, is_current, speaker, tick, tier, session FROM facts ORDER BY seq ASC",
    )).rows;
    return rows.map(rowToFact);
  }

  // ── event history (§5.4) — live over step_records ───────────────────────────
  readonly history: EventHistory = {
    append: async (rec) => {
      await this.db.query(
        "INSERT INTO step_records (run_id, step_id, attempt, rec) VALUES ($1,$2,$3,$4) ON CONFLICT (run_id, step_id, attempt) DO NOTHING",
        [rec.run_id, rec.step_id, rec.attempt, JSON.stringify(rec)],
      );
    },
    read: async (runId) => {
      const rows = (await this.db.query("SELECT rec FROM step_records WHERE run_id = $1 ORDER BY seq ASC", [runId])).rows;
      return rows.map((r) => asJson(r.rec) as StepRecord);
    },
    lookup: async (runId, stepId, attempt) => {
      const rows = (await this.db.query(
        "SELECT rec FROM step_records WHERE run_id = $1 AND step_id = $2 AND attempt = $3",
        [runId, stepId, attempt],
      )).rows;
      return rows.length ? (asJson(rows[0].rec) as StepRecord) : undefined;
    },
    lastAttempt: async (runId, stepId) => {
      const rows = (await this.db.query(
        "SELECT MAX(attempt) AS m FROM step_records WHERE run_id = $1 AND step_id = $2",
        [runId, stepId],
      )).rows;
      const m = rows[0]?.m;
      return m == null ? -1 : Number(m);
    },
  };

  // ── result cache (§5.7) — live over result_cache ────────────────────────────
  readonly cache: ResultCache = {
    get: async (key, tick) => {
      const rows = (await this.db.query("SELECT output, expires_tick FROM result_cache WHERE key = $1", [key])).rows;
      if (rows.length === 0) return undefined;
      const r = rows[0];
      if (r.expires_tick != null && tick >= Number(r.expires_tick)) return undefined;
      return asJson(r.output) as Row;
    },
    put: async (key, output, opts) => {
      const entry: CacheEntry = { output, scope: opts.scope ?? "rotor", expiresTick: opts.ttlTicks };
      await this.db.query(
        "INSERT INTO result_cache (key, output, scope, expires_tick) VALUES ($1,$2,$3,$4) " +
          "ON CONFLICT (key) DO UPDATE SET output=excluded.output, scope=excluded.scope, expires_tick=excluded.expires_tick",
        [key, JSON.stringify(output), entry.scope, entry.expiresTick ?? null],
      );
    },
  };

  // ── facts — write to SQL, query via the shared pure engine ──────────────────
  async writeFacts(facts: Fact[]): Promise<number> {
    for (const f of facts) {
      // ATOMIC supersede+insert in a single statement (a data-modifying CTE). The
      // CTE runs to completion regardless of whether the primary query reads it, so
      // the old current row is flipped and the new one inserted in one indivisible
      // step — there is no window where a concurrent reader sees the key superseded
      // with no current replacement (a torn write). Portable across `pg` Pool and
      // PGlite without pinning a connection for BEGIN/COMMIT. A null key matches no
      // rows in the CTE, so keyless facts just insert. Mirrors the SqliteStore write
      // semantics, so the closed ops (incl. `prev`) behave identically.
      await this.db.query(
        "WITH superseded AS (UPDATE facts SET is_current = FALSE WHERE fact_key = $5 AND is_current = TRUE) " +
          "INSERT INTO facts (entity, role, filler, space_id, fact_key, is_current, speaker, tick, tier, session) " +
          "VALUES ($1,$2,$3,$4,$5,TRUE,$6,$7,$8,$9)",
        [f.entity, f.role, f.filler, f.space_id ?? null, f.key ?? null, f.speaker ?? null, f.tick, f.tier ?? null, f.session ?? null],
      );
    }
    return facts.length;
  }

  async lookupFact(entity: string, role: string, spaceId?: string): Promise<Fact | undefined> {
    return lookupCurrentFact(await this.allFacts(), entity, role, spaceId);
  }
  async fillers(entity: string, role: string, spaceId?: string): Promise<string[]> {
    return currentFillers(await this.allFacts(), entity, role, spaceId);
  }
  async query(op: string, params: Row, spaceId?: string): Promise<QueryResult> {
    return runClosedOp(op, params, await this.allFacts(), spaceId);
  }
  async snapshotFacts(): Promise<Fact[]> {
    return this.allFacts();
  }

  // ── kv ──────────────────────────────────────────────────────────────────────
  async kvGet(key: string): Promise<unknown> {
    const rows = (await this.db.query("SELECT v FROM kv WHERE k = $1", [key])).rows;
    return rows.length ? asJson(rows[0].v) : undefined;
  }
  async kvSet(key: string, value: unknown): Promise<void> {
    await this.db.query(
      "INSERT INTO kv (k, v) VALUES ($1,$2) ON CONFLICT (k) DO UPDATE SET v = excluded.v",
      [key, JSON.stringify(value ?? null)],
    );
  }

  async deleteFacts(match: { entity?: string; role?: string; filler?: string; key?: string }): Promise<number> {
    const cond: string[] = [];
    const args: string[] = [];
    const bind = (col: string, v: string): void => { args.push(v); cond.push(`${col} = $${args.length}`); };
    if (match.entity !== undefined) bind("entity", match.entity);
    if (match.role !== undefined) bind("role", match.role);
    if (match.filler !== undefined) bind("filler", match.filler);
    if (match.key !== undefined) bind("fact_key", match.key);
    if (!cond.length) return 0;
    const r = await this.db.query(`DELETE FROM facts WHERE ${cond.join(" AND ")}`, args);
    return (r as { rowCount?: number }).rowCount ?? 0;
  }

  // ── sessions — atomic ordinal assignment ────────────────────────────────────
  async touchSession(id: string): Promise<number> {
    // Assign the next ordinal at insert time; a concurrent pod that loses the
    // race conflicts on the id primary key (DO NOTHING) and reads the winner.
    await this.db.query(
      "INSERT INTO sessions (id, ordinal) VALUES ($1, (SELECT COALESCE(MAX(ordinal), -1) + 1 FROM sessions)) ON CONFLICT (id) DO NOTHING",
      [id],
    );
    const rows = (await this.db.query("SELECT ordinal FROM sessions WHERE id = $1", [id])).rows;
    return rows.length ? Number(rows[0].ordinal) : 0;
  }
  async sessionOrdinal(id?: string): Promise<number> {
    if (id === undefined) {
      const rows = (await this.db.query("SELECT MAX(ordinal) AS m FROM sessions")).rows;
      return Math.max(0, rows[0]?.m == null ? 0 : Number(rows[0].m));
    }
    const rows = (await this.db.query("SELECT ordinal FROM sessions WHERE id = $1", [id])).rows;
    return rows.length ? Number(rows[0].ordinal) : 0;
  }

  /** Live writes are awaited directly, so there is nothing to flush; retained for
   *  interface symmetry with buffered sinks. */
  async flush(): Promise<void> {
    /* no-op: writes are synchronous-awaited to Postgres */
  }

  /** Release the client. */
  async shutdown(): Promise<void> {
    if (this.db.close) await this.db.close();
    else if (this.db.end) await this.db.end();
  }

  async close(): Promise<void> {
    await this.shutdown();
  }
}

/** Lazy-load node-postgres only when a real connection is needed, so `pg` is not
 *  pulled into environments that use the in-process/SQLite/injected backends.
 *  Shared with every stator-backed store (harness/threads.ts) — ONE connection
 *  mechanism, never a second. `max` caps the pool: a store that pins
 *  session-scoped state (`SET search_path`) passes 1 so every query rides the
 *  same connection. */
export async function connectPg(url?: string, opts: { max?: number } = {}): Promise<PgLike> {
  const mod: unknown = await import("pg");
  const Pool = (mod as { default?: { Pool: new (c: unknown) => unknown }; Pool?: new (c: unknown) => unknown }).Pool
    ?? (mod as { default: { Pool: new (c: unknown) => unknown } }).default.Pool;
  const pool = new Pool({ connectionString: url, ...(opts.max ? { max: opts.max } : {}) }) as {
    query(sql: string, params?: unknown[]): Promise<{ rows: Array<Record<string, unknown>> }>;
    end(): Promise<void>;
  };
  return {
    query: (sql, params) => pool.query(sql, params),
    end: () => pool.end(),
  };
}

/** JSONB comes back parsed from `pg`; PGlite may return a JSON string. */
export function asJson(v: unknown): unknown {
  return typeof v === "string" ? JSON.parse(v) : v;
}
