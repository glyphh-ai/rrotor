/**
 * The fact model + the closed-op query engine, as PURE functions over a fact
 * array (SPEC.md §7.4/§7.5, §3.1). Factored out of the in-process store so the
 * SQLite backend and the in-memory backend share byte-identical semantics — the
 * determinism guardrail (golden replay) must pass the same on both.
 *
 * There is NO model-generated SQL here by construction: the op set is closed and
 * fixed-template. Every function is deterministic given the fact list; ordering
 * decisions (most-recent-current wins; `top` ties broken by filler asc) live here
 * once so both backends agree.
 */

// ── the fact triple (entity, role, filler) ──────────────────────────────────

/** Retention tier — how long a memory lives and who recalls it (docs/memory.md).
 *  `long` = lifelong (every session); `mid` = recent sessions (a session-count
 *  window); `short` = the current session only. Absent ⇒ `long`. */
export type MemoryTier = "short" | "mid" | "long";

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
  /** Retention tier. Absent ⇒ `long`. */
  tier?: MemoryTier;
  /** The session that wrote this fact (for short/mid scoping). */
  session?: string;
}

export interface VisibilityOptions {
  /** The session recall is happening in. */
  currentSession?: string;
  /** Ordinal of a session id (monotonic; higher = more recent). */
  ordinalOf: (session?: string) => number;
  /** Mid-tier is visible within this many sessions of the current one. */
  midWindow: number;
  entity?: string;
  role?: string;
  spaceId?: string;
}

/**
 * The tier visibility filter (docs/memory.md), pure: which current facts a recall
 * in `currentSession` may see. `long` always; `short` only in its own session;
 * `mid` within `midWindow` sessions. Deterministic — session distance is measured
 * in ordinals, never wall-clock.
 */
export function visibleFacts(facts: readonly Fact[], opts: VisibilityOptions): Fact[] {
  const cur = opts.ordinalOf(opts.currentSession);
  return facts.filter((f) => {
    if (!f.is_current || !spaceMatch(f, opts.spaceId)) return false;
    if (opts.entity !== undefined && norm(f.entity) !== norm(opts.entity)) return false;
    if (opts.role !== undefined && norm(f.role) !== norm(opts.role)) return false;
    const tier: MemoryTier = f.tier ?? "long";
    if (tier === "long") return true;
    if (tier === "short") return f.session !== undefined && f.session === opts.currentSession;
    // mid: a fact with no session is treated as current; otherwise window-bounded.
    if (f.session === undefined) return true;
    return cur - opts.ordinalOf(f.session) <= opts.midWindow;
  });
}

/** A row projected out of a closed op — a plain, wire-agnostic object. */
export type Row = Record<string, unknown>;

/** The result of a closed op (SPEC.md §7.5). */
export interface QueryResult {
  rows: Row[];
  count: number;
  matched: boolean;
}

export const norm = (s: unknown): string => String(s ?? "").trim().toLowerCase();

function spaceMatch(fact: Fact, spaceId?: string): boolean {
  return spaceId === undefined || fact.space_id === undefined || fact.space_id === spaceId;
}

/** The exact current filler for `(entity, role)` — most-recent current wins. The
 *  `facts` array MUST be in write order (ascending); the last matching current
 *  fact is the newest. */
export function lookupCurrentFact(
  facts: readonly Fact[],
  entity: string,
  role: string,
  spaceId?: string,
): Fact | undefined {
  for (let i = facts.length - 1; i >= 0; i--) {
    const f = facts[i];
    if (f.is_current && norm(f.entity) === norm(entity) && norm(f.role) === norm(role) && spaceMatch(f, spaceId)) {
      return f;
    }
  }
  return undefined;
}

/** All current fillers for `(entity, role)`, in write order. */
export function currentFillers(
  facts: readonly Fact[],
  entity: string,
  role: string,
  spaceId?: string,
): string[] {
  const out: string[] = [];
  for (const f of facts) {
    if (f.is_current && norm(f.entity) === norm(entity) && norm(f.role) === norm(role) && spaceMatch(f, spaceId)) {
      out.push(f.filler);
    }
  }
  return out;
}

/** The closed, fixed-template op set — deterministic given the fact list. */
export function runClosedOp(op: string, params: Row, facts: readonly Fact[], spaceId?: string): QueryResult {
  const entity = params.person ?? params.entity;
  const role = params.slot ?? params.role;
  const current = facts.filter((f) => f.is_current && spaceMatch(f, spaceId));
  const rowsOf = (fs: Fact[]): Row[] => fs.map((f) => ({ entity: f.entity, role: f.role, filler: f.filler }));

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
      const superseded = facts.filter(
        (f) =>
          !f.is_current &&
          spaceMatch(f, spaceId) &&
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
        // Count desc, then filler asc for a deterministic tie-break.
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

/** Apply a write (with optional supersession-by-key) to a fact array in place,
 *  returning the number written. Shared by both backends' `writeFacts`. */
export function applyFactWrite(facts: Fact[], incoming: Fact[]): number {
  let n = 0;
  for (const f of incoming) {
    if (f.key) {
      for (const prior of facts) {
        if (prior.key === f.key && prior.is_current) prior.is_current = false;
      }
    }
    facts.push({ ...f, is_current: true });
    n++;
  }
  return n;
}
