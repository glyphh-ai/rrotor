/**
 * The pluggable turn-embedder seam (embedder.ts). The hash backend must stay
 * byte-for-byte identical to the historical `embed()` (replay-safe by
 * construction), the env factory selects hash-by-default / http-on-request, and
 * the HTTP backend must speak the OpenAI-compatible embeddings shape.
 */

import { describe, it, expect, vi, afterEach } from "vitest";

import { embed } from "../../src/exec/embedding.js";
import {
  HashEmbedder,
  HttpEmbedder,
  embedderFromEnv,
  DEFAULT_EMBED_DIM,
  type Embedder,
} from "../../src/exec/embedder.js";

describe("HashEmbedder", () => {
  it("is byte-for-byte identical to the historical embed() for the default dim", async () => {
    const e = new HashEmbedder();
    expect(e.dim).toBe(DEFAULT_EMBED_DIM);
    expect(await e.embed("the quick brown fox")).toEqual(embed("the quick brown fox"));
  });

  it("honours a custom dim, matching embed(text, dim)", async () => {
    const e = new HashEmbedder(64);
    expect(e.dim).toBe(64);
    expect(await e.embed("rrotor runtime")).toEqual(embed("rrotor runtime", 64));
  });

  it("embedBatch maps embed over its input", async () => {
    const e = new HashEmbedder();
    expect(await e.embedBatch(["a", "b"])).toEqual([embed("a"), embed("b")]);
  });
});

describe("embedderFromEnv", () => {
  it("defaults to a HashEmbedder(256) when nothing is set", () => {
    const e = embedderFromEnv({} as NodeJS.ProcessEnv);
    expect(e).toBeInstanceOf(HashEmbedder);
    expect(e.dim).toBe(256);
  });

  it("takes ROTOR_EMBED_DIM for the hash backend", () => {
    const e = embedderFromEnv({ ROTOR_EMBED_DIM: "128" } as unknown as NodeJS.ProcessEnv);
    expect(e).toBeInstanceOf(HashEmbedder);
    expect(e.dim).toBe(128);
  });

  it("builds an HttpEmbedder for backend=http", () => {
    const e = embedderFromEnv({
      ROTOR_EMBED_BACKEND: "http",
      ROTOR_EMBED_URL: "https://embed.example/v1/embeddings",
      ROTOR_EMBED_DIM: "1024",
    } as unknown as NodeJS.ProcessEnv);
    expect(e).toBeInstanceOf(HttpEmbedder);
    expect(e.dim).toBe(1024);
  });

  it("http requires ROTOR_EMBED_URL", () => {
    expect(() =>
      embedderFromEnv({ ROTOR_EMBED_BACKEND: "http", ROTOR_EMBED_DIM: "1024" } as unknown as NodeJS.ProcessEnv),
    ).toThrow(/ROTOR_EMBED_URL/);
  });

  it("http requires a numeric ROTOR_EMBED_DIM", () => {
    expect(() =>
      embedderFromEnv({
        ROTOR_EMBED_BACKEND: "http",
        ROTOR_EMBED_URL: "https://embed.example/v1/embeddings",
      } as unknown as NodeJS.ProcessEnv),
    ).toThrow(/ROTOR_EMBED_DIM/);
  });
});

describe("HttpEmbedder", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    delete process.env.ROTOR_EMBED_RETRIES;
    delete process.env.ROTOR_EMBED_BATCH;
  });

  it("POSTs { model, input } with bearer auth and parses data[0].embedding", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ data: [{ embedding: [0.1, 0.2, 0.3] }] }),
    }));
    vi.stubGlobal("fetch", fetchMock);

    const e: Embedder = new HttpEmbedder({
      url: "https://embed.example/v1/embeddings",
      dim: 3,
      model: "bge-large",
      apiKey: "sk-test",
    });
    const v = await e.embed("hello");
    expect(v).toEqual([0.1, 0.2, 0.3]);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://embed.example/v1/embeddings");
    expect(init.method).toBe("POST");
    const headers = init.headers as Record<string, string>;
    expect(headers.authorization).toBe("Bearer sk-test");
    expect(JSON.parse(init.body as string)).toEqual({ model: "bge-large", input: ["hello"] });
  });

  it("omits Authorization when no apiKey is set", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ data: [{ embedding: [1, 2] }] }),
    }));
    vi.stubGlobal("fetch", fetchMock);

    await new HttpEmbedder({ url: "https://embed.example/e", dim: 2, model: "gte-large" }).embed("x");
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect((init.headers as Record<string, string>).authorization).toBeUndefined();
  });

  it("throws a clear error on a non-retryable non-2xx response (no retry storm)", async () => {
    // 400 is NOT in EMBED_RETRYABLE — exactly one attempt, a clear error.
    // (503 is retryable BY DESIGN now: backoff + retry-after; this test's old
    // 503 shape predated that and gated every deploy red.)
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 400,
      text: async () => "bad request",
    }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(new HttpEmbedder({ url: "https://embed.example/e", dim: 2 }).embed("x")).rejects.toThrow(/400/);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("embedBatch sends one request for the whole batch", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ data: [{ embedding: [1] }, { embedding: [2] }] }),
    }));
    vi.stubGlobal("fetch", fetchMock);

    const out = await new HttpEmbedder({ url: "https://embed.example/e", dim: 1 }).embedBatch(["a", "b"]);
    expect(out).toEqual([[1], [2]]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(init.body as string).input).toEqual(["a", "b"]);
  });

  it("embedBatch short-circuits an empty input without hitting the network", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    expect(await new HttpEmbedder({ url: "https://embed.example/e", dim: 1 }).embedBatch([])).toEqual([]);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("embedBatch chunks inputs past ROTOR_EMBED_BATCH into ordered requests", async () => {
    process.env.ROTOR_EMBED_BATCH = "2";
    const fetchMock = vi.fn(async (_url: string, init: RequestInit) => {
      const n = (JSON.parse(init.body as string).input as string[]).length;
      return { ok: true, status: 200, json: async () => ({ data: Array.from({ length: n }, () => ({ embedding: [1] })) }) };
    });
    vi.stubGlobal("fetch", fetchMock);
    const out = await new HttpEmbedder({ url: "https://embed.example/e", dim: 1 }).embedBatch(["a", "b", "c", "d", "e"]);
    expect(out).toHaveLength(5);
    expect(fetchMock).toHaveBeenCalledTimes(3); // 2 + 2 + 1
  });

  it("retries a 429 honouring Retry-After, then succeeds", async () => {
    vi.useFakeTimers();
    let call = 0;
    const fetchMock = vi.fn(async () => {
      call++;
      if (call === 1)
        return { ok: false, status: 429, headers: { get: (h: string) => (h === "retry-after" ? "2" : null) }, text: async () => "rate limited" };
      return { ok: true, status: 200, json: async () => ({ data: [{ embedding: [9] }] }) };
    });
    vi.stubGlobal("fetch", fetchMock);
    const p = new HttpEmbedder({ url: "https://embed.example/e", dim: 1 }).embed("hi");
    await vi.runAllTimersAsync();
    await expect(p).resolves.toEqual([9]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("retries a retryable status with backoff when no Retry-After is present", async () => {
    vi.useFakeTimers();
    let call = 0;
    const fetchMock = vi.fn(async () => {
      call++;
      if (call === 1) return { ok: false, status: 503, headers: { get: () => null }, text: async () => "unavailable" };
      return { ok: true, status: 200, json: async () => ({ data: [{ embedding: [5] }] }) };
    });
    vi.stubGlobal("fetch", fetchMock);
    const p = new HttpEmbedder({ url: "https://embed.example/e", dim: 1 }).embed("hi");
    await vi.runAllTimersAsync();
    await expect(p).resolves.toEqual([5]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("retries a network-level failure, then succeeds", async () => {
    vi.useFakeTimers();
    let call = 0;
    const fetchMock = vi.fn(async () => {
      call++;
      if (call === 1) throw new Error("ECONNRESET");
      return { ok: true, status: 200, json: async () => ({ data: [{ embedding: [7] }] }) };
    });
    vi.stubGlobal("fetch", fetchMock);
    const p = new HttpEmbedder({ url: "https://embed.example/e", dim: 1 }).embed("hi");
    await vi.runAllTimersAsync();
    await expect(p).resolves.toEqual([7]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("throws after exhausting retries on a persistent network failure", async () => {
    vi.useFakeTimers();
    process.env.ROTOR_EMBED_RETRIES = "2";
    const fetchMock = vi.fn(async () => {
      throw new Error("ECONNRESET");
    });
    vi.stubGlobal("fetch", fetchMock);
    const p = new HttpEmbedder({ url: "https://embed.example/e", dim: 1 }).embed("hi").catch((e) => e);
    await vi.runAllTimersAsync();
    const err = await p;
    expect(err).toBeInstanceOf(Error);
    expect(String(err)).toMatch(/failed/);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("throws with the status once a retryable error exhausts its budget", async () => {
    process.env.ROTOR_EMBED_RETRIES = "1"; // one attempt → no retry, throw immediately
    const fetchMock = vi.fn(async () => ({ ok: false, status: 503, headers: { get: () => null }, text: async () => "unavailable" }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(new HttpEmbedder({ url: "https://embed.example/e", dim: 1 }).embed("x")).rejects.toThrow(/503/);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("throws when the endpoint returns fewer vectors than inputs", async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ data: [{ embedding: [1] }] }) }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(new HttpEmbedder({ url: "https://embed.example/e", dim: 1 }).embedBatch(["a", "b"])).rejects.toThrow(/vectors for/);
  });

  it("throws when a returned element carries no embedding array", async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ data: [{ embedding: [1] }, {}] }) }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(new HttpEmbedder({ url: "https://embed.example/e", dim: 1 }).embedBatch(["a", "b"])).rejects.toThrow(
      /no embedding at index/,
    );
  });
});
