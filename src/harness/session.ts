/**
 * harness/session.ts — one hosted harness run: frame fan-out, replay, and the
 * pause/resume seam for Ask/Approval.
 *
 * A `HarnessSession` is the pod-side identity of ONE interactive run. It owns:
 *
 *   • the frame stream: `emit()` stamps the envelope (seq/runId/at), retains
 *     the frame in the replay ring, and fans it out to live subscribers —
 *     a late subscriber replays `since(cursor)` first, so a reconnect loses
 *     nothing (in-memory v1 of the stator tape).
 *   • the pause/resume registry: an `approval`/`ask` frame parks the run on a
 *     promise keyed by the frame's `id`; `answer(id, payload)` — wired to
 *     POST /runs/:id/answer — resolves it and the loop continues. Timeouts
 *     resolve empty/deny so an unattended run re-plans instead of hanging
 *     (desktop: APPROVAL_TIMEOUT_MS / ASK_TIMEOUT_MS).
 *   • the abort seam: `stop()` aborts the run's controller, which the SDK uses
 *     to tear down the agent subprocess.
 */

import { FrameRing, isTerminal } from "./frames.js";
import type { AgentFrame, WireFrame } from "./frames.js";
import { log } from "../obs/logger.js";

/** What an answer to a paused frame carries: `allow` for approvals, `answers`
 *  for asks. Extra keys are ignored. */
export interface AnswerPayload {
  allow?: boolean;
  answers?: string[];
}

export type RunStatus = "running" | "done" | "error" | "stopped";

/** Mint a run id in the desktop's format. */
export function mintRunId(): string {
  return `run-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}`;
}

export class HarnessSession {
  readonly runId: string;
  readonly sessionId: string;
  readonly ctrl = new AbortController();
  readonly startedAt = Date.now();
  status: RunStatus = "running";

  private seq = 0;
  private readonly ring: FrameRing;
  private readonly subs = new Set<(f: WireFrame) => void>();
  private readonly pending = new Map<string, { resolve: (a: AnswerPayload) => void; timer: NodeJS.Timeout }>();
  private readonly logger;

  constructor(opts: { runId?: string; sessionId?: string; ringCap?: number; now?: () => number }) {
    this.runId = opts.runId ?? mintRunId();
    this.sessionId = opts.sessionId ?? "";
    this.ring = new FrameRing(opts.ringCap);
    this.now = opts.now ?? Date.now;
    this.logger = log.child({ run_id: this.runId });
  }

  private readonly now: () => number;

  /** Envelope + retain + fan out one frame. Returns the wire form. */
  emit(frame: AgentFrame): WireFrame {
    const wire: WireFrame = { ...frame, seq: this.seq++, runId: this.runId, at: this.now() };
    this.ring.push(wire);
    for (const fn of this.subs) {
      try {
        fn(wire);
      } catch (err) {
        // A broken subscriber never takes the run down.
        this.logger.warn("frame subscriber failed", { detail: (err as Error).message });
      }
    }
    if (isTerminal(frame)) {
      this.status = frame.type === "error" ? "error" : (frame as { stopped?: boolean }).stopped ? "stopped" : "done";
      // A run that ends with questions in flight resolves them empty — nothing
      // may wait on a dead run.
      for (const [id] of this.pending) this.answer(id, {});
    }
    return wire;
  }

  /** Replay from `from` (exclusive; -1 = everything), then receive live frames.
   *  Returns the unsubscribe function. */
  subscribe(fn: (f: WireFrame) => void, from = -1): () => void {
    for (const f of this.ring.since(from)) fn(f);
    this.subs.add(fn);
    return () => {
      this.subs.delete(fn);
    };
  }

  /** The replay tape past a cursor — the HTTP frames endpoint reads this. */
  framesSince(from = -1): WireFrame[] {
    return this.ring.since(from);
  }

  /**
   * Park the run awaiting an answer to frame `id`. The caller emits the
   * `approval`/`ask` frame; this registers the resolver. On timeout the
   * promise resolves with `fallback` (deny / no answers) — never rejects.
   */
  waitAnswer(id: string, timeoutMs: number, fallback: AnswerPayload = {}): Promise<AnswerPayload> {
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        this.logger.warn("answer timed out", { answer_id: id });
        resolve(fallback);
      }, timeoutMs);
      timer.unref?.();
      this.pending.set(id, { resolve, timer });
    });
  }

  /** Resolve a parked frame. Returns false when nothing waits under `id`. */
  answer(id: string, payload: AnswerPayload): boolean {
    const entry = this.pending.get(id);
    if (!entry) return false;
    this.pending.delete(id);
    clearTimeout(entry.timer);
    entry.resolve({
      ...(payload.allow !== undefined ? { allow: Boolean(payload.allow) } : {}),
      ...(Array.isArray(payload.answers) ? { answers: payload.answers.map(String) } : {}),
    });
    return true;
  }

  /** Ids currently awaiting an answer (surfaced by the run-status endpoint). */
  pendingIds(): string[] {
    return [...this.pending.keys()];
  }

  /** Abort the run — the engine observes the controller and emits done{stopped}. */
  stop(): void {
    this.ctrl.abort();
  }
}
