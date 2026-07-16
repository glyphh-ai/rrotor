/**
 * The SQLite-backed stator — the durable, single-node backend behind the same
 * {@link Stator} interface as {@link InProcessStore} (docs/runtime.md §2.5, §3.2;
 * SPEC.md §5.4, §5.7, §17.1).
 *
 * Why it matters: the in-process store dies with the process and is not shared
 * across requests, so the §17.1 "state in the stator, fungible pods" property and
 * cross-request replay are only real once the event history + cache + facts live
 * outside the run. This backend persists them to a SQLite file (or `:memory:`),
 * so a run recorded by one request replays from a second request, and survives a
 * process restart.
 *
 * Determinism parity: fact queries load the fact rows and run the SAME pure engine
 * as the in-process store (facts.ts), so the golden replay harness passes
 * identically on both backends. The premium tier (Postgres + pgvector, shared
 * cross-pod) swaps in behind this same interface.
 */

import Database from "better-sqlite3";

import type { StepRecord } from "../types.js";
import {
  currentFillers,
  lookupCurrentFact,
  runClosedOp,
  type Fact,
  type QueryResult,
  type Row,
} from "./facts.js";
import type { CacheEntry, EventHistory, ResultCache, Stator } from "./store.js";

type DB = InstanceType<typeof Database>;

const SCHEMA = `
CREATE TABLE IF NOT EXISTS step_records (
  seq     INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id  TEXT NOT NULL,
  step_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  rec     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_step_records_run ON step_records(run_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS ux_step_records_key ON step_records(run_id, step_id, attempt);

CREATE TABLE IF NOT EXISTS result_cache (
  key          TEXT PRIMARY KEY,
  output       TEXT NOT NULL,
  scope        TEXT NOT NULL,
  expires_tick INTEGER
);

CREATE TABLE IF NOT EXISTS facts (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  entity     TEXT NOT NULL,
  role       TEXT NOT NULL,
  filler     TEXT NOT NULL,
  space_id   TEXT,
  fact_key   TEXT,
  is_current INTEGER NOT NULL,
  speaker    TEXT,
  tick       INTEGER NOT NULL,
  tier       TEXT,
  session    TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  id      TEXT PRIMARY KEY,
  ordinal INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turns (
  seq  INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL
);
`;

interface FactRow {
  entity: string;
  role: string;
  filler: string;
  space_id: string | null;
  fact_key: string | null;
  is_current: number;
  speaker: string | null;
  tick: number;
  tier: string | null;
  session: string | null;
}

function rowToFact(r: FactRow): Fact {
  const f: Fact = {
    entity: r.entity,
    role: r.role,
    filler: r.filler,
    is_current: r.is_current === 1,
    tick: r.tick,
  };
  if (r.space_id !== null) f.space_id = r.space_id;
  if (r.fact_key !== null) f.key = r.fact_key;
  if (r.speaker !== null) f.speaker = r.speaker;
  if (r.tier !== null) f.tier = r.tier as Fact["tier"];
  if (r.session !== null) f.session = r.session;
  return f;
}

export class SqliteStore implements Stator {
  private readonly db: DB;

  constructor(path = ":memory:") {
    this.db = new Database(path);
    // WAL + a busy timeout let MULTIPLE PROCESSES share one file safely: many
    // readers concurrently, writes serialized, and a writer waits (up to 5s) for
    // a competing write instead of throwing SQLITE_BUSY. This is what makes one
    // local stator file usable by every embedded client on the machine at once.
    this.db.pragma("journal_mode = WAL");
    this.db.pragma("busy_timeout = 5000");
    this.db.exec(SCHEMA);
  }

  // ── event history (§5.4) ───────────────────────────────────────────────────
  // Async to satisfy the Stator contract; better-sqlite3 is synchronous under the
  // hood, so these resolve immediately.
  readonly history: EventHistory = {
    append: async (rec) => {
      this.db
        .prepare("INSERT OR IGNORE INTO step_records (run_id, step_id, attempt, rec) VALUES (?,?,?,?)")
        .run(rec.run_id, rec.step_id, rec.attempt, JSON.stringify(rec));
    },
    read: async (runId) => {
      const rows = this.db
        .prepare("SELECT rec FROM step_records WHERE run_id = ? ORDER BY seq ASC")
        .all(runId) as Array<{ rec: string }>;
      return rows.map((r) => JSON.parse(r.rec) as StepRecord);
    },
    lookup: async (runId, stepId, attempt) => {
      const row = this.db
        .prepare("SELECT rec FROM step_records WHERE run_id = ? AND step_id = ? AND attempt = ?")
        .get(runId, stepId, attempt) as { rec: string } | undefined;
      return row ? (JSON.parse(row.rec) as StepRecord) : undefined;
    },
    lastAttempt: async (runId, stepId) => {
      const row = this.db
        .prepare("SELECT MAX(attempt) AS m FROM step_records WHERE run_id = ? AND step_id = ?")
        .get(runId, stepId) as { m: number | null };
      return row.m ?? -1;
    },
  };

  // ── result cache (§5.7) ─────────────────────────────────────────────────────
  readonly cache: ResultCache = {
    get: async (key, tick) => {
      const row = this.db
        .prepare("SELECT output, expires_tick FROM result_cache WHERE key = ?")
        .get(key) as { output: string; expires_tick: number | null } | undefined;
      if (!row) return undefined;
      if (row.expires_tick !== null && tick >= row.expires_tick) return undefined;
      return JSON.parse(row.output) as Row;
    },
    put: async (key, output, opts) => {
      const entry: CacheEntry = { output, scope: opts.scope ?? "rotor", expiresTick: opts.ttlTicks };
      this.db
        .prepare(
          "INSERT INTO result_cache (key, output, scope, expires_tick) VALUES (?,?,?,?) " +
            "ON CONFLICT(key) DO UPDATE SET output=excluded.output, scope=excluded.scope, expires_tick=excluded.expires_tick",
        )
        .run(key, JSON.stringify(output), entry.scope, entry.expiresTick ?? null);
    },
  };

  // ── facts (§7.4/§7.5) — write to SQL, query via the shared pure engine ──────
  async writeFacts(facts: Fact[]): Promise<number> {
    const tx = this.db.transaction((incoming: Fact[]) => {
      // Reuse the shared supersession logic over the current rows, then persist
      // the resulting deltas. Simpler and parity-safe: apply per-fact directly.
      let n = 0;
      const supersede = this.db.prepare("UPDATE facts SET is_current = 0 WHERE fact_key = ? AND is_current = 1");
      const insert = this.db.prepare(
        "INSERT INTO facts (entity, role, filler, space_id, fact_key, is_current, speaker, tick, tier, session) VALUES (?,?,?,?,?,1,?,?,?,?)",
      );
      for (const f of incoming) {
        if (f.key) supersede.run(f.key);
        insert.run(f.entity, f.role, f.filler, f.space_id ?? null, f.key ?? null, f.speaker ?? null, f.tick, f.tier ?? null, f.session ?? null);
        n++;
      }
      return n;
    });
    return tx(facts);
  }

  /** All facts in write order — the input to the shared pure query engine. */
  private allFacts(): Fact[] {
    const rows = this.db
      .prepare("SELECT entity, role, filler, space_id, fact_key, is_current, speaker, tick, tier, session FROM facts ORDER BY seq ASC")
      .all() as FactRow[];
    return rows.map(rowToFact);
  }

  async snapshotFacts(): Promise<Fact[]> {
    return this.allFacts();
  }

  async touchSession(id: string): Promise<number> {
    const existing = this.db.prepare("SELECT ordinal FROM sessions WHERE id = ?").get(id) as { ordinal: number } | undefined;
    if (existing) return existing.ordinal;
    const max = this.db.prepare("SELECT MAX(ordinal) AS m FROM sessions").get() as { m: number | null };
    const ordinal = (max.m ?? -1) + 1;
    this.db.prepare("INSERT INTO sessions (id, ordinal) VALUES (?, ?)").run(id, ordinal);
    return ordinal;
  }

  async sessionOrdinal(id?: string): Promise<number> {
    if (id === undefined) {
      const max = this.db.prepare("SELECT MAX(ordinal) AS m FROM sessions").get() as { m: number | null };
      return Math.max(0, max.m ?? 0);
    }
    const row = this.db.prepare("SELECT ordinal FROM sessions WHERE id = ?").get(id) as { ordinal: number } | undefined;
    return row?.ordinal ?? 0;
  }

  async lookupFact(entity: string, role: string, spaceId?: string): Promise<Fact | undefined> {
    return lookupCurrentFact(this.allFacts(), entity, role, spaceId);
  }

  async fillers(entity: string, role: string, spaceId?: string): Promise<string[]> {
    return currentFillers(this.allFacts(), entity, role, spaceId);
  }

  async query(op: string, params: Row, spaceId?: string): Promise<QueryResult> {
    return runClosedOp(op, params, this.allFacts(), spaceId);
  }

  // ── kv scratch ──────────────────────────────────────────────────────────────
  async kvGet(key: string): Promise<unknown> {
    const row = this.db.prepare("SELECT v FROM kv WHERE k = ?").get(key) as { v: string } | undefined;
    return row ? JSON.parse(row.v) : undefined;
  }
  async kvSet(key: string, value: unknown): Promise<void> {
    this.db
      .prepare("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v")
      .run(key, JSON.stringify(value ?? null));
  }

  // ── turns ─────────────────────────────────────────────────────────────────
  async addTurn(text: string): Promise<void> {
    this.db.prepare("INSERT INTO turns (text) VALUES (?)").run(text);
  }
  async turns(): Promise<string[]> {
    const rows = this.db.prepare("SELECT text FROM turns ORDER BY seq ASC").all() as Array<{ text: string }>;
    return rows.map((r) => r.text);
  }

  async close(): Promise<void> {
    this.db.close();
  }
}
