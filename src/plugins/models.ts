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
import { cosine, embed } from "../exec/embedding.js";
import type {
  LadderRung,
  ModelRequest,
  ModelResult,
  ModelsPlugin,
} from "./interfaces.js";

export interface BasicModelsOptions {
  /** OpenAI/Anthropic-compatible local endpoint, e.g. `http://localhost:8080`. */
  modelUrl?: string;
  /** Metered frontier endpoint (§8.5); reachable only when configured. */
  frontierUrl?: string;
  /** Default local model id sent to the endpoint. */
  defaultModel?: string;
  /** Per-call timeout (ms). */
  timeoutMs?: number;
}

export class BasicModels implements ModelsPlugin {
  readonly name = "models";
  private readonly url?: string;
  private readonly frontierUrl?: string;
  private readonly defaultModel: string;
  private readonly timeoutMs: number;

  constructor(opts: BasicModelsOptions = {}) {
    this.url = opts.modelUrl ?? process.env.ROTOR_MODEL_URL ?? undefined;
    this.frontierUrl = opts.frontierUrl ?? process.env.ROTOR_FRONTIER_URL ?? undefined;
    this.defaultModel = opts.defaultModel ?? "glyphh-local";
    this.timeoutMs = opts.timeoutMs ?? 30_000;
  }

  status(): CapabilityStatus {
    const lanes = [this.url ? "local→live" : "local→stub", this.frontierUrl ? "frontier→live" : "frontier→degrade"];
    return { ready: true, detail: lanes.join("; "), tier: "basic" };
  }

  decide(lane: string | undefined): { lane: "local" | "frontier" } {
    // A frontier ask is honored only when a frontier endpoint is configured.
    return { lane: lane === "frontier" && this.frontierUrl ? "frontier" : "local" };
  }

  async execute(request: ModelRequest, lane: string): Promise<ModelResult> {
    if (lane === "frontier" && this.frontierUrl) {
      const live = await this.callEndpoint(this.frontierUrl, request, "frontier");
      if (live) return live;
      // FrontierDeclined → degrade to the local lane rather than crash (§9).
    }
    if (this.url) {
      const live = await this.callEndpoint(this.url, request, "local");
      if (live) return live;
    }
    return this.stub(request, lane);
  }

  classify(question: string, classes: string[]): { class: string; margin: number } {
    // Deterministic nearest-prototype over the local embedding (§7.10 typed decode);
    // OUT_OF_SCHEMA when the top match is below a small confidence floor.
    if (classes.length === 0) return { class: "OUT_OF_SCHEMA", margin: 0 };
    const qv = embed(question);
    const ranked = [...classes]
      .sort()
      .map((c) => ({ c, score: cosine(qv, embed(c)) }))
      .sort((a, b) => b.score - a.score || (a.c < b.c ? -1 : a.c > b.c ? 1 : 0));
    const top = ranked[0];
    const margin = ranked.length > 1 ? top.score - ranked[1].score : top.score;
    if (top.score <= 0) return { class: "OUT_OF_SCHEMA", margin: 0 };
    return { class: top.c, margin };
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

  private async callEndpoint(
    url: string,
    request: ModelRequest,
    lane: "local" | "frontier",
  ): Promise<ModelResult | undefined> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const res = await fetch(`${url}/v1/chat/completions`, {
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
        usage?: { prompt_tokens?: number; completion_tokens?: number; cost?: number };
      };
      const text = body.choices?.[0]?.message?.content ?? "";
      const usage: Usage = {
        input: body.usage?.prompt_tokens ?? tokenish(request.prompt),
        output: body.usage?.completion_tokens ?? tokenish(text),
        // Local is free by construction; only the frontier lane accrues cost.
        cost: lane === "frontier" ? (body.usage?.cost ?? 0) : 0,
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
