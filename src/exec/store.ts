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

// ───────────────────────────────────────────────────────────────────────────
// Facts — the (entity, role, filler) triples the closed ops and the exact-match
// grounder read (SPEC.md §7.4/§7.5, §3.1).
// ───────────────────────────────────────────────────────────────────────────

export interface Fact {
  /** The subject / person the fact is keyed on. */
  entity: string;
  /** The slot, e.g. `relational.object`. */
  role: string;
  /** The value stored in the slot. */
  filler: string;
  space_id?: string;
  /** Versioning chain key (§7.4); a new write with the same key supersedes. */
  key?: string;
  /** `false` once superseded within its key chain. */
  is_current: boolean;
  speaker?: string;
  /** Logical tick the fact was written at (§5.3). */
  tick: number;
}

/** A row projected out of a closed op — a plain, wire-agnostic object. */
export type Row = Record<string, unknown>;

/** The result of a closed op (SPEC.md §7.5). */
export interface QueryResult {
  rows: Row[];
  count: number;
  matched: boolean;
}

const norm = (s: unknown): string => String(s ?? "").trim().toLowerCase();

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

  // ── facts ──────────────────────────────────────────────────────────────────
  writeFacts(facts: Fact[]): number {
    let n = 0;
    for (const f of facts) {
      if (f.key) {
        for (const prior of this.facts) {
          if (prior.key === f.key && prior.is_current) prior.is_current = false;
        }
      }
      this.facts.push({ ...f, is_current: true });
      n++;
    }
    return n;
  }

  lookupFact(entity: string, role: string, spaceId?: string): Fact | undefined {
    // Most-recent current fact wins.
    for (let i = this.facts.length - 1; i >= 0; i--) {
      const f = this.facts[i];
      if (
        f.is_current &&
        norm(f.entity) === norm(entity) &&
        norm(f.role) === norm(role) &&
        (spaceId === undefined || f.space_id === undefined || f.space_id === spaceId)
      ) {
        return f;
      }
    }
    return undefined;
  }

  fillers(entity: string, role: string, spaceId?: string): string[] {
    const out: string[] = [];
    for (const f of this.facts) {
      if (
        f.is_current &&
        norm(f.entity) === norm(entity) &&
        norm(f.role) === norm(role) &&
        (spaceId === undefined || f.space_id === undefined || f.space_id === spaceId)
      ) {
        out.push(f.filler);
      }
    }
    return out;
  }

  /** The closed, fixed-template op set — deterministic given store state. */
  query(op: string, params: Row, spaceId?: string): QueryResult {
    const entity = params.person ?? params.entity;
    const role = params.slot ?? params.role;
    const current = this.facts.filter(
      (f) => f.is_current && (spaceId === undefined || f.space_id === undefined || f.space_id === spaceId),
    );
    const rowsOf = (fs: Fact[]): Row[] =>
      fs.map((f) => ({ entity: f.entity, role: f.role, filler: f.filler }));

    switch (op) {
      case "lookup": {
        const rows = rowsOf(
          current.filter(
            (f) =>
              (entity === undefined || norm(f.entity) === norm(entity)) &&
              (role === undefined || norm(f.role) === norm(role)),
          ),
        );
        return { rows, count: rows.length, matched: rows.length > 0 };
      }
      case "prev": {
        const superseded = this.facts.filter(
          (f) =>
            !f.is_current &&
            (entity === undefined || norm(f.entity) === norm(entity)) &&
            (role === undefined || norm(f.role) === norm(role)),
        );
        const rows = rowsOf(superseded);
        return { rows, count: rows.length, matched: rows.length > 0 };
      }
      case "count": {
        const n = current.filter(
          (f) =>
            (entity === undefined || norm(f.entity) === norm(entity)) &&
            (role === undefined || norm(f.role) === norm(role)),
        ).length;
        return { rows: [], count: n, matched: n > 0 };
      }
      case "count_not": {
        const n = current.filter(
          (f) =>
            (entity === undefined || norm(f.entity) !== norm(entity)) &&
            (role === undefined || norm(f.role) === norm(role)),
        ).length;
        return { rows: [], count: n, matched: n > 0 };
      }
      case "top": {
        const k = Number(params.k ?? 5);
        const counts = new Map<string, number>();
        for (const f of current) {
          if (role === undefined || norm(f.role) === norm(role)) {
            counts.set(f.filler, (counts.get(f.filler) ?? 0) + 1);
          }
        }
        const ranked = Array.from(counts.entries())
          // Sort by count desc, then filler asc for a deterministic tie-break.
          .sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
          .slice(0, k)
          .map(([filler, c]) => ({ filler, count: c }));
        return { rows: ranked, count: ranked.length, matched: ranked.length > 0 };
      }
      case "who": {
        const value = params.value;
        const rows = rowsOf(
          current.filter(
            (f) =>
              (role === undefined || norm(f.role) === norm(role)) &&
              (value === undefined || norm(f.filler) === norm(value)),
          ),
        );
        return { rows, count: rows.length, matched: rows.length > 0 };
      }
      case "compare": {
        const a = params.a;
        const b = params.b;
        const fillA = current
          .filter((f) => norm(f.entity) === norm(a) && (role === undefined || norm(f.role) === norm(role)))
          .map((f) => f.filler);
        const fillB = current
          .filter((f) => norm(f.entity) === norm(b) && (role === undefined || norm(f.role) === norm(role)))
          .map((f) => f.filler);
        const shared = fillA.filter((x) => fillB.some((y) => norm(x) === norm(y)));
        return {
          rows: [{ a, b, a_fillers: fillA, b_fillers: fillB, shared }],
          count: shared.length,
          matched: shared.length > 0,
        };
      }
      case "refuse":
      default:
        return { rows: [], count: 0, matched: false };
    }
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
