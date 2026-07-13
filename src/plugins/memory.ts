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

  /** Lexical unit-dot over recorded turns — the bare-box fallback for the
   *  embedding lane (§7.7). Ranking over recorded text is deterministic. */
  semanticRecall(query: string, topK: number, threshold: number): SemanticHit[] {
    const q = tokenSet(query);
    if (q.size === 0) return [];
    const scored: SemanticHit[] = [];
    for (const text of this.store.turns()) {
      const t = tokenSet(text);
      let overlap = 0;
      for (const w of q) if (t.has(w)) overlap++;
      const denom = Math.sqrt(q.size) * Math.sqrt(t.size || 1);
      const score = denom === 0 ? 0 : overlap / denom;
      if (score >= threshold) scored.push({ text, score });
    }
    return scored
      .sort((a, b) => b.score - a.score || (a.text < b.text ? -1 : a.text > b.text ? 1 : 0))
      .slice(0, topK);
  }

  write(
    facts: Array<Record<string, unknown>>,
    opts: { key?: string; mode?: string; speaker?: string; spaceId?: string; tick?: number },
  ): number {
    const tick = opts.tick ?? 0;
    const toWrite: Fact[] = facts.map((f) => ({
      entity: String(f.entity ?? f.subject ?? opts.key ?? "unknown"),
      role: String(f.role ?? f.slot ?? "raw.text"),
      filler: String(f.filler ?? f.value ?? ""),
      space_id: opts.spaceId,
      key: opts.key,
      is_current: true,
      speaker: opts.speaker,
      tick,
    }));
    return this.store.writeFacts(toWrite);
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

  cascade(): { short: number; mid: number; long: number } {
    // Basic tier: report the recorded turn count as the "short" tier; no
    // model summaries (mid) and no lattice absorb (long) without the premium
    // consolidation engine.
    return { short: this.store.turns().length, mid: 0, long: 0 };
  }
}

function tokenSet(s: string): Set<string> {
  return new Set(
    s
      .toLowerCase()
      .split(/[^a-z0-9]+/)
      .filter((w) => w.length > 0),
  );
}
