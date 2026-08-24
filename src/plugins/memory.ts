/**
 * BasicMemory — the in-process stator (docs/runtime.md §3.2). It wraps an
 * {@link InProcessStore}: the closed ops (§7.5), exact-match probe/verify,
 * fact writes (§7.4), the append-only event
 * history (§5.4), and the result cache (§5.7). Zero-dependency Maps — no SQLite,
 * no Postgres. Degrades to this single-node backend rather than crashing when no
 * shared stator is configured; the premium swap-in is Postgres + pgvector behind
 * the same interface.
 */

import type { CapabilityStatus } from "../runtime/registry.js";
import type { StepRecord } from "../types.js";
import { InProcessStore, type Fact, type Row, type Stator } from "../exec/store.js";
import { visibleFacts, type MemoryTier } from "../exec/facts.js";
import type {
  GroundVerdict,
  MemoryPlugin,
  ProbeResult,
  QueryResult,
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

  /** CURATION surface for the Memory panel — raw reads + targeted deletes. */
  async snapshot(): Promise<Fact[]> {
    return this.store.snapshotFacts();
  }
  async deleteFacts(match: { entity?: string; role?: string; filler?: string; key?: string }): Promise<number> {
    return this.store.deleteFacts(match);
  }

  /** The conversation window lives in the stator's KV, keyed by session —
   *  session-scoped by construction, ordered by append, bounded to the newest
   *  CONV_CAP exchanges. Deterministic: an ordinal read, no embeddings. */
  private static readonly CONV_CAP = 20;
  async appendConversation(session: string, speaker: string, text: string): Promise<void> {
    if (!text || text.trim() === "") return;
    const key = `conv:${session}`;
    const prior = ((await this.store.kvGet(key)) as Array<{ speaker: string; text: string }> | undefined) ?? [];
    const next = [...prior, { speaker, text }].slice(-BasicMemory.CONV_CAP);
    await this.store.kvSet(key, next);
  }

  async conversation(session: string, k: number): Promise<Array<{ speaker: string; text: string }>> {
    const prior = ((await this.store.kvGet(`conv:${session}`)) as Array<{ speaker: string; text: string }> | undefined) ?? [];
    return prior.slice(-Math.max(1, k));
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

}
