/**
 * The attention meter — deterministic enforcement of the §10 attention budget.
 *
 * A budget is a **control input**: `on_exhausted` changes a run's transitions. In
 * a replayable system a control input MUST be a pure function of recorded state,
 * never wall-clock (SPEC.md §6, §17.6). So this meter bounds only the three
 * deterministic dimensions — **revolutions** (loop re-entries), **tokens**, and
 * **cost** — all recomputable from the event history, so a fresh run and its
 * replay make the identical exhaustion decision.
 *
 * `AttentionBudget.wall_ms` is deliberately NOT enforced here: a wall-clock bound
 * cannot be a determinism-safe control predicate (it would diverge on replay). It
 * is a transport/operational signal, handled — if at all — outside the control
 * plane. See docs/observability.md / BUILD_PLAN.md Phase 4.
 */

import type { AttentionBudget, AttentionOnExhausted, Usage } from "../types.js";

export type BudgetReason = "revolutions" | "tokens" | "cost";

export interface BudgetOutcome {
  on: AttentionOnExhausted;
  reason: BudgetReason;
  revolutions: number;
  tokens: number;
  cost: number;
}

export class AttentionMeter {
  private revolutions = 0;
  private tokens = 0;
  private cost = 0;

  constructor(private readonly budget?: AttentionBudget) {}

  /** Count one loop revolution (a transition back into an already-run step). */
  revolution(): void {
    this.revolutions++;
  }

  /** Accrue a step's recorded usage (deterministic — from the StepRecord). */
  record(usage?: Usage): void {
    if (!usage) return;
    this.tokens += (usage.input ?? 0) + (usage.output ?? 0);
    this.cost += usage.cost ?? 0;
  }

  /** The deterministic exhaustion decision, or `undefined` while within budget. */
  exhausted(): BudgetOutcome | undefined {
    const b = this.budget;
    if (!b) return undefined;
    const on = b.on_exhausted ?? "stop";
    if (b.revolutions !== undefined && this.revolutions >= b.revolutions) {
      return { on, reason: "revolutions", ...this.snapshot() };
    }
    if (b.tokens !== undefined && this.tokens > b.tokens) {
      return { on, reason: "tokens", ...this.snapshot() };
    }
    if (b.cost !== undefined && this.cost > b.cost) {
      return { on, reason: "cost", ...this.snapshot() };
    }
    return undefined;
  }

  snapshot(): { revolutions: number; tokens: number; cost: number } {
    return { revolutions: this.revolutions, tokens: this.tokens, cost: this.cost };
  }
}
