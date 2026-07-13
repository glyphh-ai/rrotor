/**
 * BasicModels — the quarantined data-plane executor (docs/runtime.md §3.3). It
 * talks to a local OpenAI-compatible endpoint when `ROTOR_MODEL_URL` is set, and
 * otherwise degrades to a deterministic zero-model stub: a geometric-fluency
 * ranker that, given grounded candidates, echoes the first one so the loop still
 * grounds/refuses; absent candidates it echoes the prompt. Only the LOCAL lane
 * is free by construction; frontier is the metering point (and is unavailable on
 * a bare box — `FrontierDeclined` falls back to local, never crashes).
 *
 * Output tokens are recorded, never promised reproducible (§6.2).
 */

import type { CapabilityStatus } from "../runtime/registry.js";
import type { Frame, Usage } from "../types.js";
import { tokenish } from "../exec/util.js";
import type {
  LadderRung,
  ModelRequest,
  ModelResult,
  ModelsPlugin,
} from "./interfaces.js";

export interface BasicModelsOptions {
  /** OpenAI/Anthropic-compatible local endpoint, e.g. `http://localhost:8080`. */
  modelUrl?: string;
  /** Default local model id sent to the endpoint. */
  defaultModel?: string;
  /** Per-call timeout (ms). */
  timeoutMs?: number;
}

export class BasicModels implements ModelsPlugin {
  readonly name = "models";
  private readonly url?: string;
  private readonly defaultModel: string;
  private readonly timeoutMs: number;

  constructor(opts: BasicModelsOptions = {}) {
    this.url = opts.modelUrl ?? process.env.ROTOR_MODEL_URL ?? undefined;
    this.defaultModel = opts.defaultModel ?? "glyphh-local";
    this.timeoutMs = opts.timeoutMs ?? 30_000;
  }

  status(): CapabilityStatus {
    return {
      ready: true,
      detail: this.url ? `local lane → ${this.url}` : "local lane only (zero-model stub)",
      tier: "basic",
    };
  }

  decide(lane: string | undefined): { lane: "local" | "frontier" } {
    // Basic tier serves only the free local lane; a frontier ask degrades.
    return { lane: lane === "frontier" ? "frontier" : "local" };
  }

  async execute(request: ModelRequest, lane: string): Promise<ModelResult> {
    if (this.url && lane !== "frontier") {
      const live = await this.callLocal(request);
      if (live) return live;
    }
    return this.stub(request, lane);
  }

  classify(question: string, classes: string[]): { class: string; margin: number } {
    // Deterministic nearest-prototype by lexical overlap; OUT_OF_SCHEMA on a tie
    // at zero overlap (§7.11 note).
    const q = new Set(question.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean));
    let best = "OUT_OF_SCHEMA";
    let bestScore = 0;
    let second = 0;
    for (const c of [...classes].sort()) {
      let overlap = 0;
      for (const w of c.toLowerCase().split(/[^a-z0-9]+/)) if (q.has(w)) overlap++;
      if (overlap > bestScore) {
        second = bestScore;
        bestScore = overlap;
        best = c;
      } else if (overlap > second) {
        second = overlap;
      }
    }
    return { class: best, margin: bestScore - second };
  }

  escalate(_trigger: string, to?: string, fallback?: string): { lane: LadderRung } {
    // Frontier is unreachable on a bare box → fall back per config (§9).
    if (to === "frontier" && !this.url) {
      const fb = (fallback ?? "local") as LadderRung;
      return { lane: fb === "frontier" ? "local" : fb };
    }
    if (to === "human") return { lane: "human" };
    return { lane: (to as LadderRung) ?? "local" };
  }

  // ── internals ──────────────────────────────────────────────────────────────

  private stub(request: ModelRequest, lane: string): ModelResult {
    let text: string;
    if (request.candidates && request.candidates.length > 0) {
      // Zero-model ranker: pick the first grounded continuation deterministically.
      text = [...request.candidates].sort()[0];
    } else {
      text = `[stub:${lane}] ${request.prompt}`.slice(0, 2000);
    }
    const frames: Frame[] = [
      { type: "propose", data: { text } },
      { type: "done" },
    ];
    const usage: Usage = { input: tokenish(request.prompt), output: tokenish(text), cost: 0 };
    return { text, frames, usage };
  }

  private async callLocal(request: ModelRequest): Promise<ModelResult | undefined> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const res = await fetch(`${this.url}/v1/chat/completions`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          model: request.model ?? this.defaultModel,
          messages: [{ role: "user", content: request.prompt }],
          temperature: request.temperature ?? 0,
          seed: request.seed,
        }),
        signal: controller.signal,
      });
      if (!res.ok) return undefined;
      const body = (await res.json()) as {
        choices?: Array<{ message?: { content?: string } }>;
        usage?: { prompt_tokens?: number; completion_tokens?: number };
      };
      const text = body.choices?.[0]?.message?.content ?? "";
      const usage: Usage = {
        input: body.usage?.prompt_tokens ?? tokenish(request.prompt),
        output: body.usage?.completion_tokens ?? tokenish(text),
        cost: 0,
      };
      const frames: Frame[] = [
        { type: "propose", data: { text } },
        { type: "done" },
      ];
      return { text, frames, usage };
    } catch {
      // Any transport error degrades to the stub (graceful degradation, §3.9).
      return undefined;
    } finally {
      clearTimeout(timer);
    }
  }
}
