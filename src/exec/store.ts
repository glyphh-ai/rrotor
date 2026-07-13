/**
 * The in-process stator — the single source of truth for a run (docs/runtime.md
 * §2.5, §3.2). It holds the append-only **event history** (StepRecords, §5.4),
 * a **fact store** for the deterministic closed ops (`retrieve.sql`, §7.5) and
 * exact-match grounding (§3.1 basic), a **key/value** scratch, and a **result
 * cache** (§5.7).
 *
 * This is the BASIC, zero-dependency backend: plain Maps, no SQLite, no
 * Postgres. It is durable for the lifetime of the process and single-node —
 * enough for L2 execution and single-node L3 replay. The premium swap-in
 * (SQLite / Postgres + pgvector, shared cross-pod) lives behind the same
 * `MemoryPlugin` interface.
 */

import type { StepRecord } from "../types.js";
import {
  applyFactWrite,
  currentFillers,
  lookupCurrentFact,
  runClosedOp,
  type Fact,
  type QueryResult,
  type Row,
} from "./facts.js";

// The fact model + closed-op engine live in facts.ts and are shared with the
// SQLite backend so both agree byte-for-byte. Re-exported for existing importers.
export type { Fact, QueryResult, Row } from "./facts.js";

// ───────────────────────────────────────────────────────────────────────────
// The event history (SPEC.md §5.4) — append-only, keyed by run.
// ───────────────────────────────────────────────────────────────────────────

export interface EventHistory {
  append(rec: StepRecord): Promise<void>;
  read(runId: string): Promise<StepRecord[]>;
  /** Replay lookup: the record for `(step_id, attempt)` if one exists (§5.4). */
  lookup(runId: string, stepId: string, attempt: number): Promise<StepRecord | undefined>;
  /** Highest recorded attempt for a step, or `-1` if none (retry bookkeeping). */
  lastAttempt(runId: string, stepId: string): Promise<number>;
}

// ───────────────────────────────────────────────────────────────────────────
// The result cache (SPEC.md §5.7). A hit is recorded into the StepRecord at
// first execution and replay never re-consults the cache — so this basic Map is
// only ever read on FIRST execution.
// ───────────────────────────────────────────────────────────────────────────

export interface CacheEntry {
  output: Row;
  scope: string;
  /** Tick at which the entry expires; `undefined` = no tick-based expiry. */
  expiresTick?: number;
}

export interface ResultCache {
  get(key: string, tick: number): Promise<Row | undefined>;
  put(key: string, output: Row, opts: { ttlTicks?: number; scope?: string }): Promise<void>;
}

// ───────────────────────────────────────────────────────────────────────────
// The Stator — the object graph the memory plugin exposes.
// ───────────────────────────────────────────────────────────────────────────

export interface Stator {
  readonly history: EventHistory;
  readonly cache: ResultCache;
  /** Persist facts; supersedes any current fact sharing a `key`. Returns count. */
  writeFacts(facts: Fact[]): Promise<number>;
  /** Exact current filler for `(entity, role)` — the basic grounder's source. */
  lookupFact(entity: string, role: string, spaceId?: string): Promise<Fact | undefined>;
  /** All current fillers for `(entity, role)` — the hard-gate mask source. */
  fillers(entity: string, role: string, spaceId?: string): Promise<string[]>;
  /** The closed op set (SPEC.md §7.5). NO model-generated SQL by construction. */
  query(op: string, params: Row, spaceId?: string): Promise<QueryResult>;
  /** KV scratch. */
  kvGet(key: string): Promise<unknown>;
  kvSet(key: string, value: unknown): Promise<void>;
  /** Turn-level text for `retrieve.vector` lexical fallback (§7.7). */
  addTurn(text: string): Promise<void>;
  turns(): Promise<string[]>;
  /** Register a session id (assigning a monotonic ordinal) and mark it current.
   *  Ordinals order sessions for mid-tier windowing (docs/memory.md). */
  touchSession(id: string): Promise<number>;
  /** The ordinal of a session id; the latest ordinal for `undefined`. */
  sessionOrdinal(id?: string): Promise<number>;
  /** All facts — the input to the tier visibility filter. */
  snapshotFacts(): Promise<Fact[]>;
  /** Release backing resources (file handles). No-op for in-process. */
  close?(): Promise<void>;
}

// ───────────────────────────────────────────────────────────────────────────
// The in-process implementation.
// ───────────────────────────────────────────────────────────────────────────

export class InProcessStore implements Stator {
  private readonly recs: StepRecord[] = [];
  private readonly cacheMap = new Map<string, CacheEntry>();
  private readonly facts: Fact[] = [];
  private readonly kv = new Map<string, unknown>();
  private readonly turnLog: string[] = [];
  private readonly sessions = new Map<string, number>();
  private sessionCounter = 0;

  // ── event history ────────────────────────────────────────────────────────
  // Methods are async to satisfy the Stator contract (which durable/networked
  // backends need); the in-process impl resolves synchronously.
  readonly history: EventHistory = {
    append: async (rec) => {
      this.recs.push(rec);
    },
    read: async (runId) => this.recs.filter((r) => r.run_id === runId),
    lookup: async (runId, stepId, attempt) =>
      this.recs.find(
        (r) => r.run_id === runId && r.step_id === stepId && r.attempt === attempt,
      ),
    lastAttempt: async (runId, stepId) => {
      let max = -1;
      for (const r of this.recs) {
        if (r.run_id === runId && r.step_id === stepId && r.attempt > max) max = r.attempt;
      }
      return max;
    },
  };

  // ── result cache ───────────────────────────────────────────────────────────
  readonly cache: ResultCache = {
    get: async (key, tick) => {
      const e = this.cacheMap.get(key);
      if (!e) return undefined;
      if (e.expiresTick !== undefined && tick >= e.expiresTick) return undefined;
      return e.output;
    },
    put: async (key, output, opts) => {
      this.cacheMap.set(key, {
        output,
        scope: opts.scope ?? "rotor",
        expiresTick: opts.ttlTicks,
      });
    },
  };

  // ── facts (delegated to the shared pure engine in facts.ts) ────────────────
  async writeFacts(facts: Fact[]): Promise<number> {
    return applyFactWrite(this.facts, facts);
  }

  async lookupFact(entity: string, role: string, spaceId?: string): Promise<Fact | undefined> {
    return lookupCurrentFact(this.facts, entity, role, spaceId);
  }

  async fillers(entity: string, role: string, spaceId?: string): Promise<string[]> {
    return currentFillers(this.facts, entity, role, spaceId);
  }

  /** The closed, fixed-template op set — deterministic given store state. */
  async query(op: string, params: Row, spaceId?: string): Promise<QueryResult> {
    return runClosedOp(op, params, this.facts, spaceId);
  }

  // ── kv + turns ───────────────────────────────────────────────────────────
  async kvGet(key: string): Promise<unknown> {
    return this.kv.get(key);
  }
  async kvSet(key: string, value: unknown): Promise<void> {
    this.kv.set(key, value);
  }
  async addTurn(text: string): Promise<void> {
    this.turnLog.push(text);
  }
  async turns(): Promise<string[]> {
    return this.turnLog.slice();
  }

  // ── sessions + fact snapshot ────────────────────────────────────────────────
  async touchSession(id: string): Promise<number> {
    let o = this.sessions.get(id);
    if (o === undefined) {
      o = this.sessionCounter++;
      this.sessions.set(id, o);
    }
    return o;
  }
  async sessionOrdinal(id?: string): Promise<number> {
    if (id === undefined) return Math.max(0, this.sessionCounter - 1);
    return this.sessions.get(id) ?? 0;
  }
  async snapshotFacts(): Promise<Fact[]> {
    return this.facts.slice();
  }
}
