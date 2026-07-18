/**
 * `crypto` tool pack — deterministic cryptographic primitives: digests, HMAC,
 * RFC 4122 name-based UUIDs, seeded pseudo-randomness, and JWT inspection.
 *
 * Every tool here is `pure` with `grants: []` — a deterministic function of its
 * inputs and nothing else. That is the whole point of the pack: hashing, HMAC and
 * uuid.v5 are deterministic by definition; `random.seeded` is a hand-rolled
 * xorshift128 driven ONLY by the caller's seed string (never `Math.random`, never
 * `crypto.randomBytes` — nothing that would make a replayed tape diverge); and
 * `jwt.decode` deliberately does NOT verify signatures (verification needs a key
 * and a trust decision — decoding is inspection, and the output says so with
 * `verified: false`). Inputs are bounded (text/token size caps, value-count caps)
 * so a tool result can never flood the tape.
 */

import { createHash, createHmac } from "node:crypto";

import { RotorError } from "../errors.js";
import type { Row } from "../plugins/interfaces.js";
import type { ToolPack, ToolSpec } from "./spec.js";

const MAX_TEXT = 1_000_000; // 1 MB of input is plenty for an agent-facing digest
const MAX_TOKEN = 32_768;
const MAX_VALUES = 1_000;

/** Resolve the bytes to digest: `text` as UTF-8, or base64-decoded when `b64`. */
function inputBytes(args: Row, tool: string): Buffer {
  const text = args.text;
  if (typeof text !== "string") throw new RotorError("E_MISSING_INPUT", `${tool} requires a string \`text\``);
  if (text.length > MAX_TEXT) {
    throw new RotorError("E_MISSING_INPUT", `${tool}: \`text\` exceeds ${MAX_TEXT} chars`, { context: { length: text.length } });
  }
  if (args.b64) {
    if (!/^[A-Za-z0-9+/]*={0,2}$/.test(text.replace(/\s/g, ""))) {
      throw new RotorError("E_MISSING_INPUT", `${tool}: \`text\` is not valid base64`);
    }
    return Buffer.from(text, "base64");
  }
  return Buffer.from(text, "utf8");
}

function hashTool(algo: "sha256" | "sha1" | "md5", description: string): ToolSpec {
  return {
    name: `hash.${algo}`,
    version: 1,
    description,
    effect: "pure",
    grants: [],
    input: {
      type: "object",
      properties: {
        text: { type: "string", description: "The data to hash (UTF-8 unless `b64`)." },
        b64: { type: "boolean", description: "Treat `text` as base64-encoded bytes." },
      },
      required: ["text"],
    },
    output: { type: "object", properties: { hex: { type: "string" } } },
    handler: async (args: Row) => ({ hex: createHash(algo).update(inputBytes(args, `hash.${algo}`)).digest("hex") }),
  };
}

// RFC 4122 Appendix C — the pre-defined name-space IDs.
const WELL_KNOWN_NS: Record<string, string> = {
  dns: "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
  url: "6ba7b811-9dad-11d1-80b4-00c04fd430c8",
  oid: "6ba7b812-9dad-11d1-80b4-00c04fd430c8",
};
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function formatUuid(b: Buffer): string {
  const h = b.toString("hex");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20, 32)}`;
}

/** Deterministic xorshift128 whose 128-bit state is derived from the seed string. */
function seededGenerator(seed: string): () => number {
  const d = createHash("sha256").update(seed, "utf8").digest();
  let x = d.readUInt32BE(0);
  let y = d.readUInt32BE(4);
  let z = d.readUInt32BE(8);
  let w = d.readUInt32BE(12);
  if ((x | y | z | w) === 0) w = 0x9e3779b9; // xorshift must never sit at the all-zero fixed point
  const next = (): number => {
    const t = (x ^ (x << 11)) >>> 0;
    x = y;
    y = z;
    z = w;
    w = (w ^ (w >>> 19) ^ t ^ (t >>> 8)) >>> 0;
    return w;
  };
  for (let i = 0; i < 8; i++) next(); // decorrelate from the raw digest words
  return next;
}

function b64urlJson(part: string, which: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(Buffer.from(part, "base64url").toString("utf8"));
  } catch {
    throw new RotorError("E_MISSING_INPUT", `jwt.decode: ${which} is not base64url-encoded JSON`);
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new RotorError("E_MISSING_INPUT", `jwt.decode: ${which} is not a JSON object`);
  }
  return parsed as Record<string, unknown>;
}

export function cryptoPack(): ToolPack {
  const tools: ToolSpec[] = [
    hashTool("sha256", "SHA-256 hex digest of `text` (UTF-8, or base64 bytes with b64:true)."),
    hashTool("sha1", "SHA-1 hex digest of `text` — for checksums/interop only (SHA-1 is broken for security use)."),
    hashTool("md5", "MD5 hex digest of `text` — for checksums/interop only (MD5 is broken for security use)."),
    {
      name: "hmac.sign",
      version: 1,
      description: "HMAC of `text` keyed by `key` (algo: sha256|sha1); returns the hex digest.",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: {
          algo: { type: "string", enum: ["sha256", "sha1"] },
          key: { type: "string" },
          text: { type: "string" },
        },
        required: ["algo", "key", "text"],
      },
      output: { type: "object", properties: { hex: { type: "string" } } },
      handler: async (args: Row) => {
        const algo = args.algo;
        if (algo !== "sha256" && algo !== "sha1") {
          throw new RotorError("E_MISSING_INPUT", "hmac.sign: `algo` must be sha256 or sha1", { context: { algo: String(algo) } });
        }
        if (typeof args.key !== "string" || args.key === "") throw new RotorError("E_MISSING_INPUT", "hmac.sign requires a non-empty string `key`");
        if (typeof args.text !== "string") throw new RotorError("E_MISSING_INPUT", "hmac.sign requires a string `text`");
        if (args.text.length > MAX_TEXT) {
          throw new RotorError("E_MISSING_INPUT", `hmac.sign: \`text\` exceeds ${MAX_TEXT} chars`, { context: { length: args.text.length } });
        }
        return { hex: createHmac(algo, args.key).update(args.text, "utf8").digest("hex") };
      },
    },
    {
      name: "uuid.v5",
      version: 1,
      description: "Deterministic RFC 4122 v5 (SHA-1 name-based) UUID from a namespace (a UUID, or dns|url|oid) and a name.",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: {
          namespace: { type: "string", description: "A namespace UUID, or one of the well-known ids: dns, url, oid." },
          name: { type: "string" },
        },
        required: ["namespace", "name"],
      },
      output: { type: "object", properties: { uuid: { type: "string" } } },
      handler: async (args: Row) => {
        if (typeof args.namespace !== "string" || typeof args.name !== "string") {
          throw new RotorError("E_MISSING_INPUT", "uuid.v5 requires string `namespace` and `name`");
        }
        const ns = WELL_KNOWN_NS[args.namespace.toLowerCase()] ?? args.namespace;
        if (!UUID_RE.test(ns)) {
          throw new RotorError("E_MISSING_INPUT", "uuid.v5: `namespace` must be a UUID or one of dns|url|oid", { context: { namespace: args.namespace } });
        }
        if (args.name.length > MAX_TEXT) {
          throw new RotorError("E_MISSING_INPUT", `uuid.v5: \`name\` exceeds ${MAX_TEXT} chars`, { context: { length: args.name.length } });
        }
        // RFC 4122 §4.3: sha1(namespace-bytes || name-bytes), then stamp version 5 + the RFC variant.
        const nsBytes = Buffer.from(ns.replace(/-/g, ""), "hex");
        const digest = createHash("sha1").update(nsBytes).update(Buffer.from(args.name, "utf8")).digest();
        const b = Buffer.from(digest.subarray(0, 16));
        b[6] = (b[6] & 0x0f) | 0x50;
        b[8] = (b[8] & 0x3f) | 0x80;
        return { uuid: formatUuid(b) };
      },
    },
    {
      name: "random.seeded",
      version: 1,
      description: "Deterministic pseudo-random numbers from `seed` (xorshift128) — same seed, same values, replay-safe. Floats in [min,max) or, with integers:true, ints in [min,max].",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: {
          seed: { type: "string", description: "The seed string; fully determines the output." },
          count: { type: "integer", minimum: 1, maximum: MAX_VALUES, description: "How many values (default 1, capped)." },
          min: { type: "number", description: "Lower bound (default 0)." },
          max: { type: "number", description: "Upper bound (default 1)." },
          integers: { type: "boolean", description: "Return integers in [min, max] inclusive." },
        },
        required: ["seed"],
      },
      output: {
        type: "object",
        properties: { values: { type: "array", items: { type: "number" } }, count: { type: "number" }, truncated: { type: "boolean" } },
      },
      handler: async (args: Row) => {
        if (typeof args.seed !== "string" || args.seed === "") throw new RotorError("E_MISSING_INPUT", "random.seeded requires a non-empty string `seed`");
        const rawCount = args.count == null ? 1 : args.count;
        if (typeof rawCount !== "number" || !Number.isInteger(rawCount) || rawCount < 1) {
          throw new RotorError("E_MISSING_INPUT", "random.seeded: `count` must be a positive integer");
        }
        const truncated = rawCount > MAX_VALUES;
        const count = truncated ? MAX_VALUES : rawCount;
        const min = args.min == null ? 0 : args.min;
        const max = args.max == null ? 1 : args.max;
        if (typeof min !== "number" || typeof max !== "number" || !Number.isFinite(min) || !Number.isFinite(max) || min >= max) {
          throw new RotorError("E_MISSING_INPUT", "random.seeded: `min` and `max` must be finite numbers with min < max", { context: { min: String(min), max: String(max) } });
        }
        const next = seededGenerator(args.seed);
        const values: number[] = [];
        if (args.integers) {
          const lo = Math.ceil(min);
          const hi = Math.floor(max);
          if (hi < lo) throw new RotorError("E_MISSING_INPUT", "random.seeded: no integers exist in [min, max]", { context: { min, max } });
          const span = hi - lo + 1;
          for (let i = 0; i < count; i++) values.push(lo + (next() % span));
        } else {
          for (let i = 0; i < count; i++) values.push(min + (next() / 2 ** 32) * (max - min));
        }
        return { values, count: values.length, truncated };
      },
    },
    {
      name: "jwt.decode",
      version: 1,
      description: "Decode a JWT's header and payload WITHOUT verifying the signature — inspection only, claims are untrusted (`verified` is always false).",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { token: { type: "string" } }, required: ["token"] },
      output: {
        type: "object",
        properties: {
          header: { type: "object" },
          payload: { type: "object" },
          signature_b64: { type: "string" },
          verified: { type: "boolean", const: false, description: "Always false: this tool never verifies signatures." },
        },
      },
      handler: async (args: Row) => {
        if (typeof args.token !== "string" || args.token.trim() === "") throw new RotorError("E_MISSING_INPUT", "jwt.decode requires a string `token`");
        const token = args.token.trim();
        if (token.length > MAX_TOKEN) {
          throw new RotorError("E_MISSING_INPUT", `jwt.decode: token exceeds ${MAX_TOKEN} chars`, { context: { length: token.length } });
        }
        const parts = token.split(".");
        if (parts.length !== 3) {
          throw new RotorError("E_MISSING_INPUT", "jwt.decode: token is not a 3-segment JWT (header.payload.signature)", { context: { segments: parts.length } });
        }
        return {
          header: b64urlJson(parts[0], "header"),
          payload: b64urlJson(parts[1], "payload"),
          signature_b64: parts[2],
          verified: false,
        };
      },
    },
  ];

  return { name: "crypto", version: "1.0.0", tools };
}
