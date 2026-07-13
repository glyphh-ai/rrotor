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
  append(rec: StepRecord): void;
  read(runId: string): StepRecord[];
  /** Replay lookup: the record for `(step_id, attempt)` if one exists (§5.4). */
  lookup(runId: string, stepId: string, attempt: number): StepRecord | undefined;
  /** Highest recorded attempt for a step, or `-1` if none (retry bookkeeping). */
  lastAttempt(runId: string, stepId: string): number;
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
  get(key: string, tick: number): Row | undefined;
  put(key: string, output: Row, opts: { ttlTicks?: number; scope?: string }): void;
}

// ───────────────────────────────────────────────────────────────────────────
// The Stator — the object graph the memory plugin exposes.
// ───────────────────────────────────────────────────────────────────────────

export interface Stator {
  readonly history: EventHistory;
  readonly cache: ResultCache;
  /** Persist facts; supersedes any current fact sharing a `key`. Returns count. */
  writeFacts(facts: Fact[]): number;
  /** Exact current filler for `(entity, role)` — the basic grounder's source. */
  lookupFact(entity: string, role: string, spaceId?: string): Fact | undefined;
  /** All current fillers for `(entity, role)` — the hard-gate mask source. */
  fillers(entity: string, role: string, spaceId?: string): string[];
  /** The closed op set (SPEC.md §7.5). NO model-generated SQL by construction. */
  query(op: string, params: Row, spaceId?: string): QueryResult;
  /** KV scratch. */
  kvGet(key: string): unknown;
  kvSet(key: string, value: unknown): void;
  /** Turn-level text for `retrieve.vector` lexical fallback (§7.7). */
  addTurn(text: string): void;
  turns(): string[];
  /** Release backing resources (file handles). No-op for in-process. */
  close?(): void;
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

  // ── event history ────────────────────────────────────────────────────────
  readonly history: EventHistory = {
    append: (rec) => {
      this.recs.push(rec);
    },
    read: (runId) => this.recs.filter((r) => r.run_id === runId),
    lookup: (runId, stepId, attempt) =>
      this.recs.find(
        (r) => r.run_id === runId && r.step_id === stepId && r.attempt === attempt,
      ),
    lastAttempt: (runId, stepId) => {
      let max = -1;
      for (const r of this.recs) {
        if (r.run_id === runId && r.step_id === stepId && r.attempt > max) max = r.attempt;
      }
      return max;
    },
  };

  // ── result cache ───────────────────────────────────────────────────────────
  readonly cache: ResultCache = {
    get: (key, tick) => {
      const e = this.cacheMap.get(key);
      if (!e) return undefined;
      if (e.expiresTick !== undefined && tick >= e.expiresTick) return undefined;
      return e.output;
    },
    put: (key, output, opts) => {
      this.cacheMap.set(key, {
        output,
        scope: opts.scope ?? "rotor",
        expiresTick: opts.ttlTicks,
      });
    },
  };

  // ── facts (delegated to the shared pure engine in facts.ts) ────────────────
  writeFacts(facts: Fact[]): number {
    return applyFactWrite(this.facts, facts);
  }

  lookupFact(entity: string, role: string, spaceId?: string): Fact | undefined {
    return lookupCurrentFact(this.facts, entity, role, spaceId);
  }

  fillers(entity: string, role: string, spaceId?: string): string[] {
    return currentFillers(this.facts, entity, role, spaceId);
  }

  /** The closed, fixed-template op set — deterministic given store state. */
  query(op: string, params: Row, spaceId?: string): QueryResult {
    return runClosedOp(op, params, this.facts, spaceId);
  }

  // ── kv + turns ───────────────────────────────────────────────────────────
  kvGet(key: string): unknown {
    return this.kv.get(key);
  }
  kvSet(key: string, value: unknown): void {
    this.kv.set(key, value);
  }
  addTurn(text: string): void {
    this.turnLog.push(text);
  }
  turns(): string[] {
    return this.turnLog.slice();
  }
}
