/**
 * The turn-embedding seam — a **pluggable embedder** behind one small interface,
 * exactly like the {@link Stator} backend is pluggable (stator.ts). The default is
 * the zero-dependency deterministic hash embedder (embedding.ts), so the open
 * runtime stays self-contained and replay-safe by construction; the hosted fleet
 * points {@link embedderFromEnv} at a real embedding model over HTTP.
 *
 * The seam is `dim` + async `embed(text)` (async so an HTTP backend fits; the hash
 * backend resolves synchronously). Callers await it uniformly and never care which
 * backend produced the vector — the pgvector column width is derived from
 * `embedder.dim`.
 */

import { embed as hashEmbed } from "./embedding.js";

/** A turn-embedding backend. `dim` is the fixed output width (the pgvector column
 *  width); `embed` maps text → a `dim`-length vector. `embedBatch` is a
 *  convenience default that maps `embed` over its input — an HTTP backend may
 *  override it to embed a whole batch in one request. */
export interface Embedder {
  readonly dim: number;
  embed(text: string): Promise<number[]>;
  embedBatch(texts: string[]): Promise<number[][]>;
}

/** Default embedder dimension — the historical {@link hashEmbed} width. */
export const DEFAULT_EMBED_DIM = 256;

/** The deterministic, dependency-free default: the existing FNV-1a hash embedding
 *  (embedding.ts). Byte-for-byte identical to calling `embed(text, dim)` directly,
 *  so existing behaviour and golden replay are unchanged. Resolves synchronously. */
export class HashEmbedder implements Embedder {
  readonly dim: number;
  constructor(dim: number = DEFAULT_EMBED_DIM) {
    this.dim = dim;
  }
  async embed(text: string): Promise<number[]> {
    return hashEmbed(text, this.dim);
  }
  async embedBatch(texts: string[]): Promise<number[][]> {
    return texts.map((t) => hashEmbed(t, this.dim));
  }
}

/** Config for {@link HttpEmbedder} (all sourced from `ROTOR_EMBED_*` env). */
export interface HttpEmbedderOptions {
  /** The embeddings endpoint (OpenAI-compatible `/v1/embeddings` shape). */
  url: string;
  /** The model's output dimension — validates/sizes the pgvector column. */
  dim: number;
  /** The model id (e.g. `bge-large`, `gte-large`). */
  model?: string;
  /** Optional bearer token, sent as `Authorization: Bearer …` when set. */
  apiKey?: string;
}

/**
 * An HTTP embedder that POSTs to an OpenAI-compatible embeddings endpoint:
 * `POST ${url}` with `{ model, input }`, reading `data[0].embedding` back. Uses
 * the global `fetch` (Node 20).
 *
 * Transient failures (429 rate-limit, 5xx, network) are retried with bounded
 * backoff honouring `Retry-After`, because hosted embedding APIs impose tight
 * request-rate caps (e.g. Mistral's 60 req/min): under batch ingest a 429 must
 * pace-and-continue, not fail the whole write. Non-transient errors (auth,
 * bad request, malformed response) still throw immediately — a dead endpoint
 * fails fast rather than storming. Tune with `ROTOR_EMBED_RETRIES` (default 6).
 *
 * NOTE: a neural embedding is NOT replay-safe on its own; the fleet checkpoints it
 * at the embedding boundary (§7.7). This class is just the transport.
 */
const EMBED_RETRYABLE = new Set([408, 409, 429, 500, 502, 503, 504, 529]);

export class HttpEmbedder implements Embedder {
  readonly dim: number;
  private readonly url: string;
  private readonly model: string;
  private readonly apiKey?: string;

  constructor(opts: HttpEmbedderOptions) {
    this.url = opts.url;
    this.dim = opts.dim;
    this.model = opts.model ?? "";
    this.apiKey = opts.apiKey;
  }

  async embed(text: string): Promise<number[]> {
    const [v] = await this.post([text]);
    return v;
  }

  async embedBatch(texts: string[]): Promise<number[][]> {
    if (texts.length === 0) return [];
    // Chunk so a large recall (hundreds of turns) doesn't exceed the endpoint's
    // per-request input/token cap. ROTOR_EMBED_BATCH overrides the chunk size.
    const chunk = Number(process.env.ROTOR_EMBED_BATCH) || 64;
    if (texts.length <= chunk) return this.post(texts);
    const out: number[][] = [];
    for (let i = 0; i < texts.length; i += chunk) {
      const vecs = await this.post(texts.slice(i, i + chunk));
      for (const v of vecs) out.push(v);
    }
    return out;
  }

  /** One POST for a batch of inputs → the ordered embedding vectors. Retries
   *  transient failures (rate-limit / 5xx / network) with backoff; throws on
   *  non-transient errors and after the attempt budget is exhausted. */
  private async post(input: string[]): Promise<number[][]> {
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (this.apiKey) headers.authorization = `Bearer ${this.apiKey}`;
    const body = JSON.stringify({ model: this.model, input });
    const maxAttempts = Number(process.env.ROTOR_EMBED_RETRIES) || 6;

    let lastErr: unknown;
    for (let attempt = 0; attempt < maxAttempts; attempt++) {
      let res: Response;
      try {
        res = await fetch(this.url, { method: "POST", headers, body });
      } catch (e) {
        // Network-level failure: transient, retry with backoff.
        lastErr = new Error(`embedding request to ${this.url} failed: ${(e as Error).message}`);
        if (attempt < maxAttempts - 1) {
          await sleep(embedBackoff(attempt));
          continue;
        }
        throw lastErr;
      }
      if (!res.ok) {
        if (EMBED_RETRYABLE.has(res.status) && attempt < maxAttempts - 1) {
          const ra = Number(res.headers?.get?.("retry-after"));
          await sleep(Number.isFinite(ra) && ra > 0 ? ra * 1000 : embedBackoff(attempt));
          continue;
        }
        const errBody = await res.text().catch(() => "");
        throw new Error(`embedding endpoint ${this.url} returned ${res.status}${errBody ? `: ${errBody.slice(0, 200)}` : ""}`);
      }

      const json = (await res.json()) as { data?: Array<{ embedding?: number[] }> };
      const data = json.data;
      if (!Array.isArray(data) || data.length < input.length) {
        throw new Error(`embedding endpoint ${this.url} returned ${data?.length ?? 0} vectors for ${input.length} inputs`);
      }
      return data.map((d, i) => {
        const v = d?.embedding;
        if (!Array.isArray(v)) throw new Error(`embedding endpoint ${this.url} returned no embedding at index ${i}`);
        return v;
      });
    }
    throw lastErr instanceof Error ? lastErr : new Error(`embedding endpoint ${this.url} failed after ${maxAttempts} attempts`);
  }
}

function embedBackoff(attempt: number): number {
  return Math.min(1000 * 2 ** attempt, 30_000) + Math.floor(Math.random() * 500);
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

/** Embedder backend selector — the mirror of `ROTOR_STATOR_BACKEND`. */
export type EmbedBackend = "hash" | "http";

/**
 * Build the turn embedder from the environment (`ROTOR_EMBED_*`), the single edit
 * site the fleet configures:
 *
 * - `ROTOR_EMBED_BACKEND=hash|http` (default `hash`).
 * - hash: `dim = ROTOR_EMBED_DIM ?? 256`.
 * - http: requires `ROTOR_EMBED_URL` and `ROTOR_EMBED_DIM` (the model's output
 *   dim); optional `ROTOR_EMBED_MODEL`, `ROTOR_EMBED_API_KEY`.
 *
 * Nothing but the default hash embedder is created unless `ROTOR_EMBED_BACKEND=http`
 * is set, so the open runtime is byte-identical to today out of the box.
 */
export function embedderFromEnv(env: NodeJS.ProcessEnv = process.env): Embedder {
  const backend: EmbedBackend = env.ROTOR_EMBED_BACKEND === "http" ? "http" : "hash";

  if (backend === "http") {
    const url = env.ROTOR_EMBED_URL;
    if (!url) throw new Error("ROTOR_EMBED_BACKEND=http requires ROTOR_EMBED_URL");
    const dim = env.ROTOR_EMBED_DIM ? Number(env.ROTOR_EMBED_DIM) : NaN;
    if (!Number.isFinite(dim) || dim <= 0) {
      throw new Error("ROTOR_EMBED_BACKEND=http requires a numeric ROTOR_EMBED_DIM (the model's output dimension)");
    }
    return new HttpEmbedder({ url, dim, model: env.ROTOR_EMBED_MODEL, apiKey: env.ROTOR_EMBED_API_KEY });
  }

  const dim = env.ROTOR_EMBED_DIM ? Number(env.ROTOR_EMBED_DIM) : DEFAULT_EMBED_DIM;
  return new HashEmbedder(Number.isFinite(dim) && dim > 0 ? dim : DEFAULT_EMBED_DIM);
}
