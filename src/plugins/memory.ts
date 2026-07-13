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

  executeOp(op: string, params: Row, spaceId?: string): QueryResult {
    return this.store.query(op, params, spaceId);
  }

  /** Deterministic-local semantic recall (§7.7): cosine over the hashed-ngram
   *  embedding of the query and each recorded turn. Replay-safe (pure). */
  semanticRecall(query: string, topK: number, threshold: number): SemanticHit[] {
    const qv = embed(query);
    if (query.trim() === "") return [];
    const scored: SemanticHit[] = [];
    for (const text of this.store.turns()) {
      const score = cosine(qv, embed(text));
      if (score >= threshold) scored.push({ text, score });
    }
    return scored
      .sort((a, b) => b.score - a.score || (a.text < b.text ? -1 : a.text > b.text ? 1 : 0))
      .slice(0, topK);
  }

  recordTurn(text: string): void {
    if (text && text.trim() !== "") this.store.addTurn(text);
  }

  write(
    facts: Array<Record<string, unknown>>,
    opts: { key?: string; mode?: string; speaker?: string; spaceId?: string; tick?: number; session?: string; tier?: MemoryTier },
  ): number {
    const tick = opts.tick ?? 0;
    if (opts.session) this.store.touchSession(opts.session);
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
      // Per-fact tier (from the absorb enricher) wins; else the step's default.
      tier: (f.tier as MemoryTier | undefined) ?? opts.tier,
      session: opts.session,
    }));
    return this.store.writeFacts(toWrite);
  }

  recall(opts: { entity?: string; role?: string; session?: string; midWindow?: number; spaceId?: string }): Fact[] {
    return visibleFacts(this.store.snapshotFacts(), {
      currentSession: opts.session,
      ordinalOf: (s) => this.store.sessionOrdinal(s),
      midWindow: opts.midWindow ?? 5,
      entity: opts.entity,
      role: opts.role,
      spaceId: opts.spaceId,
    });
  }

  probe(entity: string, role: string, spaceId?: string): ProbeResult {
    const f = this.store.lookupFact(entity, role, spaceId);
    if (!f) return { filler: null, membership: 0, margin: 0, top: [] };
    return { filler: f.filler, membership: 1, margin: 1, top: [f.filler] };
  }

  verify(entity: string, role: string, filler: string, _margin: number, spaceId?: string): GroundVerdict {
    const fillers = this.store.fillers(entity, role, spaceId);
    if (fillers.length === 0) return { grounded: false, membership: 0, margin: 0, top: [] };
    const cand = norm(filler);
    const hit = fillers.some((v) => {
      const n = norm(v);
      return n === cand || cand.includes(n) || n.includes(cand);
    });
    return { grounded: hit, membership: hit ? 1 : 0, margin: hit ? 1 : 0, top: fillers };
  }

  appendStepRecord(rec: StepRecord): void {
    this.store.history.append(rec);
  }
  readEventHistory(runId: string): StepRecord[] {
    return this.store.history.read(runId);
  }
  lookupRecord(runId: string, stepId: string, attempt: number): StepRecord | undefined {
    return this.store.history.lookup(runId, stepId, attempt);
  }
  lastAttempt(runId: string, stepId: string): number {
    return this.store.history.lastAttempt(runId, stepId);
  }

  cacheGet(key: string, tick: number): Row | undefined {
    return this.store.cache.get(key, tick);
  }
  cachePut(key: string, output: Row, opts: { ttlTicks?: number; scope?: string }): void {
    this.store.cache.put(key, output, opts);
  }

  /**
   * Short → mid → long consolidation (§7.18), deterministic over recorded turns.
   * The `span` most-recent turns are the hot **short** tier; older turns
   * consolidate into the **mid** tier by de-duplication (distinct summaries); the
   * duplicates that collapse away are the **long**-tier absorb count. A model
   * summarizer is the premium swap-in; this keeps it pure and replay-safe.
   */
  cascade(span = 8): { short: number; mid: number; long: number } {
    const turns = this.store.turns();
    const window = Math.max(0, span);
    const short = Math.min(turns.length, window);
    const older = turns.slice(0, Math.max(0, turns.length - window));
    const distinctOlder = new Set(older).size;
    return { short, mid: distinctOlder, long: older.length - distinctOlder };
  }
}
