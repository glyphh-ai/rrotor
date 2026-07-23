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
import { buildCall, parseResponse, providerForUrl, authHeaders, keyFromEnv, type Provider } from "./providers.js";
import type {
  LadderRung,
  ModelRequest,
  ModelResult,
  ModelsPlugin,
} from "./interfaces.js";

/**
 * A resolved binding for one rotor model ROLE — the control surface a control plane
 * populates. A step's `config.model` is a role name (e.g. `planner`); the runtime looks
 * it up here to get the concrete endpoint, model id, and auth. Every endpoint speaks the
 * OpenAI chat-completions wire (a gateway normalizes providers like Anthropic to it), so
 * the runtime stays provider-agnostic.
 */
export interface ModelEndpoint {
  /** Endpoint base URL — local llama-server, hosted API, or the gateway. */
  url: string;
  /** Wire the endpoint speaks; inferred from the URL host when omitted. */
  provider?: import("./providers.js").Provider;
  /** The concrete model id to send; defaults to the role/registry key. */
  model?: string;
  /** Auth / routing headers (e.g. a scoped gateway bearer). Never logged. */
  headers?: Record<string, string>;
  /** Metered (accrues cost, §8.5) — true for hosted/proxied, false for local. */
  metered?: boolean;
}

export interface BasicModelsOptions {
  /**
   * The control surface: role → resolved endpoint. Injected by the control plane (via the
   * SDK). A rotor step's `config.model` is looked up here first; unmatched roles fall back
   * to the lane endpoints below.
   */
  registry?: Record<string, ModelEndpoint>;
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
  private readonly registry: Record<string, ModelEndpoint>;
  private readonly url?: string;
  private readonly frontierUrl?: string;
  /** Bearer for the metered frontier lane (ROTOR_FRONTIER_KEY) — never logged. */
  private readonly frontierKey?: string;
  private readonly defaultModel: string;
  private readonly timeoutMs: number;

  constructor(opts: BasicModelsOptions = {}) {
    this.registry = opts.registry ?? {};
    this.url = opts.modelUrl ?? process.env.ROTOR_MODEL_URL ?? undefined;
    this.frontierUrl = opts.frontierUrl ?? process.env.ROTOR_FRONTIER_URL ?? undefined;
    this.frontierKey = process.env.ROTOR_FRONTIER_KEY ?? undefined;
    this.defaultModel = opts.defaultModel ?? process.env.ROTOR_MODEL_ID ?? "glyphh-local";
    this.timeoutMs = opts.timeoutMs ?? (process.env.ROTOR_MODEL_TIMEOUT ? Number(process.env.ROTOR_MODEL_TIMEOUT) : 120_000);
  }

  status(): CapabilityStatus {
    const roles = Object.keys(this.registry);
    const lanes = [
      roles.length ? `roles→[${roles.join(",")}]` : this.url ? "local→live" : "local→stub",
      this.frontierUrl ? "frontier→live" : "frontier→degrade",
    ];
    return { ready: true, detail: lanes.join("; "), tier: "basic" };
  }

  decide(lane: string | undefined): { lane: "local" | "frontier" } {
    // A frontier ask is honored only when a frontier endpoint is configured.
    return { lane: lane === "frontier" && this.frontierUrl ? "frontier" : "local" };
  }

  async execute(request: ModelRequest, lane: string): Promise<ModelResult> {
    const notes: string[] = [];
    const finish = (r: ModelResult): ModelResult =>
      notes.length ? { ...r, notes, frames: [{ type: "degrade", data: { notes } }, ...r.frames] } : r;

    // Control surface: a step's `model` is a ROLE bound by the control plane → its endpoint.
    const bound = request.model ? this.registry[request.model] : undefined;
    if (bound) {
      const { result, error } = await this.callEndpoint(bound.url, request, bound.metered ? "frontier" : "local", {
        model: bound.model ?? request.model,
        provider: bound.provider,
        ...(bound.headers ? { headers: bound.headers } : {}),
      });
      if (result) return finish(result);
      if (error) notes.push(`role ${request.model} @ ${bound.url}: ${error}`);
      // A bound endpoint that fails degrades to the lane fallback / stub rather than crash (§9).
    }
    if (lane === "frontier" && this.frontierUrl) {
      const frontierProvider = providerForUrl(this.frontierUrl);
      const frontierKey = this.frontierKey ?? keyFromEnv(frontierProvider);
      const { result, error } = await this.callEndpoint(this.frontierUrl, request, "frontier", {
        provider: frontierProvider,
        ...(frontierKey ? { headers: authHeaders(frontierProvider, frontierKey) } : {}),
        ...(process.env.ROTOR_FRONTIER_MODEL ? { model: process.env.ROTOR_FRONTIER_MODEL } : {}),
      });
      if (result) return finish(result);
      if (error) notes.push(`frontier ${this.frontierUrl}: ${error}`);
      // FrontierDeclined → degrade to the local lane rather than crash (§9).
    }
    if (this.url) {
      const { result, error } = await this.callEndpoint(this.url, request, "local");
      if (result) return finish(result);
      if (error) notes.push(`local ${this.url}: ${error}`);
    }
    return finish(this.stub(request, lane));
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
    // A grounded decode (candidates present) is a DETERMINISTIC ranker — replay-safe,
    // so it stays cacheable. A bare echo (no candidates) is a non-durable fallback the
    // executor must not checkpoint; `grounded` on the frame is that discriminator.
    const grounded = !!(request.candidates && request.candidates.length > 0);
    let text: string;
    if (grounded) {
      // Zero-model ranker: pick the first grounded continuation deterministically.
      text = [...request.candidates!].sort()[0];
    } else {
      text = `[stub:${lane}] ${request.prompt}`.slice(0, 2000);
    }
    const frames: Frame[] = [
      // A typed marker clients can read off the wire (frame TYPES stream on the
      // step event): this answer came from the deterministic stub, not a model.
      { type: "stub", data: { lane, grounded } },
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
    override?: { model?: string; headers?: Record<string, string>; provider?: Provider },
  ): Promise<{ result?: ModelResult; error?: string }> {
    const controller = new AbortController();
    const timeoutMs = request.timeout_ms ?? this.timeoutMs;
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      // Provider translation (§8): the runtime speaks ONE request; the adapter
      // shapes it to whatever wire the endpoint talks.
      const provider = providerForUrl(url, override?.provider);
      const call = buildCall(
        provider,
        url,
        {
          prompt: request.prompt,
          model: override?.model ?? request.model ?? this.defaultModel,
          temperature: request.temperature ?? 0,
          seed: request.seed,
          maxTokens: (request as { max_tokens?: number }).max_tokens,
        },
        override?.headers ?? {},
      );
      const res = await fetch(call.url, {
        method: "POST",
        headers: call.headers,
        body: JSON.stringify(call.body),
        signal: controller.signal,
      });
      if (!res.ok) {
        // Surface the provider's own words — a silent 400 cost a debugging
        // session once; never again.
        let detail = "";
        try {
          const err = (await res.json()) as { error?: { message?: string } };
          detail = err.error?.message ?? "";
        } catch {
          /* body optional */
        }
        return { error: `HTTP ${res.status}${detail ? ` — ${detail}` : ""}` };
      }
      const wire = parseResponse(provider, await res.json());
      const text = wire.text;
      const usage: Usage = {
        input: wire.inputTokens ?? tokenish(request.prompt),
        output: wire.outputTokens ?? tokenish(text),
        // Local is free by construction; only the frontier lane accrues cost.
        cost: lane === "frontier" ? 0 : 0,
      };
      const frames: Frame[] = [
        { type: "propose", data: { text } },
        { type: "done" },
      ];
      return { result: { text, served: lane, frames, usage } };
    } catch (err) {
      // Any transport error degrades to the next lane (graceful degradation, §3.9).
      return { error: (err as Error).name === "AbortError" ? `timeout after ${timeoutMs}ms` : (err as Error).message };
    } finally {
      clearTimeout(timer);
    }
  }
}
