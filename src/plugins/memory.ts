/**
 * BasicMemory — the in-process stator (docs/runtime.md §3.2). It wraps an
 * {@link InProcessStore}: the closed ops (§7.5), exact-match probe/verify,
 * lexical semantic recall (§7.7), fact writes (§7.4), the append-only event
 * history (§5.4), and the result cache (§5.7). Zero-dependency Maps — no SQLite,
 * no Postgres. Degrades to this single-node backend rather than crashing when no
 * shared stator is configured; the premium swap-in is Postgres + pgvector behind
 * the same interface.
 */

import type { CapabilityStatus } from "../runtime/registry.js";
import type { StepRecord } from "../types.js";
import { InProcessStore, type Fact, type Row, type Stator } from "../exec/store.js";
import { visibleFacts, type MemoryTier } from "../exec/facts.js";
import { cosine, embed } from "../exec/embedding.js";
import type {
  GroundVerdict,
  MemoryPlugin,
  ProbeResult,
  QueryResult,
  SemanticHit,
} from "./interfaces.js";

const norm = (s: unknown): string => String(s ?? "").trim().toLowerCase();

export class BasicMemory implements MemoryPlugin {
  readonly name = "memory";

  constructor(readonly store: Stator = new InProcessStore()) {}

  status(): CapabilityStatus {
    return { ready: true, detail: "in-process maps; no pgvector", tier: "basic" };
  }

  async executeOp(op: string, params: Row, spaceId?: string): Promise<QueryResult> {
    return this.store.query(op, params, spaceId);
  }

  /** Deterministic-local semantic recall (§7.7): cosine over the hashed-ngram
   *  embedding of the query and each recorded turn. Replay-safe (pure). */
  async semanticRecall(query: string, topK: number, threshold: number): Promise<SemanticHit[]> {
    const qv = embed(query);
    if (query.trim() === "") return [];
    const scored: SemanticHit[] = [];
    for (const text of await this.store.turns()) {
      const score = cosine(qv, embed(text));
      if (score >= threshold) scored.push({ text, score });
    }
    return scored
      .sort((a, b) => b.score - a.score || (a.text < b.text ? -1 : a.text > b.text ? 1 : 0))
      .slice(0, topK);
  }

  async recordTurn(text: string): Promise<void> {
    if (text && text.trim() !== "") await this.store.addTurn(text);
  }

  async write(
    facts: Array<Record<string, unknown>>,
    opts: { key?: string; mode?: string; speaker?: string; spaceId?: string; tick?: number; session?: string; tier?: MemoryTier },
  ): Promise<number> {
    const tick = opts.tick ?? 0;
    if (opts.session) await this.store.touchSession(opts.session);
    const toWrite: Fact[] = facts.map((f) => ({
      entity: String(f.entity ?? f.subject ?? opts.key ?? "unknown"),
      role: String(f.role ?? f.slot ?? "raw.text"),
      filler: String(f.filler ?? f.value ?? ""),
      space_id: opts.spaceId,
      // A per-fact key versions its own (entity, role) slot; fall back to the
      // step-level key. This keeps a multi-fact absorb from superseding itself.
      key: (f.key as string | undefined) ?? opts.key,
      is_current: true,
      speaker: opts.speaker,
      tick,
      // Precedence: the step's EXPLICIT tier is the author's deterministic
      // override and wins; else the per-fact tier from the absorb enricher; else
      // undefined (defaults to `long` at the visibility filter).
      tier: opts.tier ?? (f.tier as MemoryTier | undefined),
      session: opts.session,
    }));
    return this.store.writeFacts(toWrite);
  }

  async recall(opts: { entity?: string; role?: string; session?: string; midWindow?: number; spaceId?: string }): Promise<Fact[]> {
    const facts = await this.store.snapshotFacts();
    // Pre-resolve session ordinals so `visibleFacts` (a pure, sync filter) reads
    // them from a map instead of awaiting mid-scan.
    const curOrd = await this.store.sessionOrdinal(opts.session);
    const ord = new Map<string, number>();
    for (const f of facts) {
      if (f.session && !ord.has(f.session)) ord.set(f.session, await this.store.sessionOrdinal(f.session));
    }
    return visibleFacts(facts, {
      currentSession: opts.session,
      ordinalOf: (s) => (s === opts.session || s === undefined ? curOrd : ord.get(s) ?? 0),
      midWindow: opts.midWindow ?? 5,
      entity: opts.entity,
      role: opts.role,
      spaceId: opts.spaceId,
    });
  }

  async probe(entity: string, role: string, spaceId?: string): Promise<ProbeResult> {
    const f = await this.store.lookupFact(entity, role, spaceId);
    if (!f) return { filler: null, membership: 0, margin: 0, top: [] };
    return { filler: f.filler, membership: 1, margin: 1, top: [f.filler] };
  }

  async verify(entity: string, role: string, filler: string, _margin: number, spaceId?: string): Promise<GroundVerdict> {
    const fillers = await this.store.fillers(entity, role, spaceId);
    if (fillers.length === 0) return { grounded: false, membership: 0, margin: 0, top: [] };
    const cand = norm(filler);
    const hit = fillers.some((v) => {
      const n = norm(v);
      return n === cand || cand.includes(n) || n.includes(cand);
    });
    return { grounded: hit, membership: hit ? 1 : 0, margin: hit ? 1 : 0, top: fillers };
  }

  async appendStepRecord(rec: StepRecord): Promise<void> {
    await this.store.history.append(rec);
  }
  async readEventHistory(runId: string): Promise<StepRecord[]> {
    return this.store.history.read(runId);
  }
  async lookupRecord(runId: string, stepId: string, attempt: number): Promise<StepRecord | undefined> {
    return this.store.history.lookup(runId, stepId, attempt);
  }
  async lastAttempt(runId: string, stepId: string): Promise<number> {
    return this.store.history.lastAttempt(runId, stepId);
  }

  async cacheGet(key: string, tick: number): Promise<Row | undefined> {
    return this.store.cache.get(key, tick);
  }
  async cachePut(key: string, output: Row, opts: { ttlTicks?: number; scope?: string }): Promise<void> {
    await this.store.cache.put(key, output, opts);
  }

  /**
   * Short → mid → long consolidation (§7.18), deterministic over recorded turns.
   * The `span` most-recent turns are the hot **short** tier; older turns
   * consolidate into the **mid** tier by de-duplication (distinct summaries); the
   * duplicates that collapse away are the **long**-tier absorb count. A model
   * summarizer is the premium swap-in; this keeps it pure and replay-safe.
   */
  async cascade(span = 8): Promise<{ short: number; mid: number; long: number }> {
    const turns = await this.store.turns();
    const window = Math.max(0, span);
    const short = Math.min(turns.length, window);
    const older = turns.slice(0, Math.max(0, turns.length - window));
    const distinctOlder = new Set(older).size;
    return { short, mid: distinctOlder, long: older.length - distinctOlder };
  }
}
