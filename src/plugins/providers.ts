/**
 * providers.ts — the provider wire adapters (§8 gateway: MCP↔API↔provider
 * translation). A model endpoint speaks ONE of the supported wires; these pure
 * functions translate the runtime's ModelRequest to each provider's request
 * shape and parse its response back — no I/O here, so every adapter is
 * unit-testable without a network.
 *
 * Supported:
 *   openai    — the chat-completions wire (OpenAI, llama.cpp, vLLM, Ollama,
 *               Groq, Mistral, xAI, …). The default.
 *   anthropic — the Claude Messages API (x-api-key + anthropic-version).
 */

export type Provider = "openai" | "anthropic";

/** Infer the provider from an endpoint URL when not declared. */
export function providerForUrl(url: string, declared?: Provider): Provider {
  if (declared) return declared;
  try {
    return new URL(url).host.endsWith("anthropic.com") ? "anthropic" : "openai";
  } catch {
    return "openai";
  }
}

/** The conventional env key for a provider (ANTHROPIC_API_KEY / OPENAI_API_KEY). */
export function keyFromEnv(provider: Provider): string | undefined {
  return provider === "anthropic" ? process.env.ANTHROPIC_API_KEY : process.env.OPENAI_API_KEY;
}

/** Auth headers for a raw key, shaped per provider. */
export function authHeaders(provider: Provider, key: string): Record<string, string> {
  return provider === "anthropic"
    ? { "x-api-key": key, "anthropic-version": "2023-06-01" }
    : { authorization: `Bearer ${key}` };
}

export interface WireCall {
  /** Full request URL (endpoint base + provider path). */
  url: string;
  headers: Record<string, string>;
  body: unknown;
}

/** Build the provider request for a single-prompt completion. */
export function buildCall(
  provider: Provider,
  base: string,
  req: { prompt: string; model: string; temperature?: number; seed?: number; maxTokens?: number },
  headers: Record<string, string>,
): WireCall {
  const root = base.replace(/\/+$/, "");
  if (provider === "anthropic") {
    return {
      url: `${root}/v1/messages`,
      headers: { "content-type": "application/json", "anthropic-version": "2023-06-01", ...headers },
      // No `temperature`: current Claude models reject it (HTTP 400,
      // "temperature is deprecated"). The runtime never promised reproducible
      // tokens anyway (§6.2) — determinism lives in the control plane.
      body: {
        model: req.model,
        max_tokens: req.maxTokens ?? 2048,
        messages: [{ role: "user", content: req.prompt }],
      },
    };
  }
  return {
    url: `${root}/v1/chat/completions`,
    headers: { "content-type": "application/json", ...headers },
    body: {
      model: req.model,
      messages: [{ role: "user", content: req.prompt }],
      temperature: req.temperature ?? 0,
      seed: req.seed,
    },
  };
}

export interface WireResult {
  text: string;
  inputTokens?: number;
  outputTokens?: number;
}

/** Parse the provider response into text + token usage. */
export function parseResponse(provider: Provider, body: unknown): WireResult {
  if (provider === "anthropic") {
    const b = body as {
      content?: Array<{ type?: string; text?: string }>;
      usage?: { input_tokens?: number; output_tokens?: number };
    };
    const text = (b.content ?? [])
      .filter((c) => c.type === "text" && typeof c.text === "string")
      .map((c) => c.text)
      .join("");
    return { text, inputTokens: b.usage?.input_tokens, outputTokens: b.usage?.output_tokens };
  }
  const b = body as {
    choices?: Array<{ message?: { content?: string } }>;
    usage?: { prompt_tokens?: number; completion_tokens?: number };
  };
  return {
    text: b.choices?.[0]?.message?.content ?? "",
    inputTokens: b.usage?.prompt_tokens,
    outputTokens: b.usage?.completion_tokens,
  };
}
