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

  it("throws a clear error on a non-2xx response (no retry storm)", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 503,
      text: async () => "upstream down",
    }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(new HttpEmbedder({ url: "https://embed.example/e", dim: 2 }).embed("x")).rejects.toThrow(/503/);
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
});
