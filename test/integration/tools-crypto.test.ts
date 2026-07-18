/**
 * `crypto` pack — every tool is pure, so the tests pin exact known-answer values
 * (NIST digest vectors, the RFC 4122 dns/example.com UUID) and the pack's headline
 * property: identical inputs always produce identical outputs (replay-safe).
 */

import { describe, it, expect } from "vitest";

import { BasicConnections } from "../../src/plugins/connections.js";
import { cryptoPack } from "../../src/tools/crypto.js";

function withPack() {
  const c = new BasicConnections();
  for (const t of cryptoPack().tools) c.register(t.name, t.handler);
  return c;
}
const call = async (c: BasicConnections, name: string, args: Record<string, unknown>) => {
  const r = await c.dispatch(name, args);
  if (!r.ok) throw new Error(r.error);
  return r.result as Record<string, unknown>;
};

describe("crypto pack — hash.*", () => {
  const c = withPack();

  it("sha256/sha1/md5 match the known vectors for 'abc'", async () => {
    expect((await call(c, "hash.sha256", { text: "abc" })).hex).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    expect((await call(c, "hash.sha1", { text: "abc" })).hex).toBe("a9993e364706816aba3e25717850c26c9cd0d89d");
    expect((await call(c, "hash.md5", { text: "abc" })).hex).toBe("900150983cd24fb0d6963f7d28e17f72");
  });

  it("b64:true hashes the decoded bytes ('YWJj' === 'abc')", async () => {
    const plain = await call(c, "hash.sha256", { text: "abc" });
    const b64 = await call(c, "hash.sha256", { text: "YWJj", b64: true });
    expect(b64.hex).toBe(plain.hex);
  });

  it("refuses a missing text and invalid base64", async () => {
    await expect(c.dispatch("hash.sha256", {})).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_MISSING_INPUT/) });
    await expect(c.dispatch("hash.md5", { text: "not!!base64", b64: true })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_MISSING_INPUT/) });
  });
});

describe("crypto pack — hmac.sign", () => {
  const c = withPack();

  it("matches the RFC 2202/4231 quick-brown-fox vector", async () => {
    const r = await call(c, "hmac.sign", { algo: "sha256", key: "key", text: "The quick brown fox jumps over the lazy dog" });
    expect(r.hex).toBe("f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8");
    const r1 = await call(c, "hmac.sign", { algo: "sha1", key: "key", text: "The quick brown fox jumps over the lazy dog" });
    expect(r1.hex).toBe("de7c9b85b8b78aa6bc8a7a36f70a90701c9db4d9");
  });

  it("refuses an unsupported algo and an empty key", async () => {
    await expect(c.dispatch("hmac.sign", { algo: "md5", key: "k", text: "x" })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_MISSING_INPUT/) });
    await expect(c.dispatch("hmac.sign", { algo: "sha256", key: "", text: "x" })).resolves.toMatchObject({ ok: false });
  });
});

describe("crypto pack — uuid.v5", () => {
  const c = withPack();

  it("produces the RFC 4122 dns/example.com UUID, via alias and explicit namespace", async () => {
    const known = "cfbff0d1-9375-5685-968c-48ce8b15ae17";
    expect((await call(c, "uuid.v5", { namespace: "dns", name: "example.com" })).uuid).toBe(known);
    expect((await call(c, "uuid.v5", { namespace: "6ba7b810-9dad-11d1-80b4-00c04fd430c8", name: "example.com" })).uuid).toBe(known);
  });

  it("is deterministic, version 5, RFC variant — for url and oid namespaces too", async () => {
    for (const namespace of ["url", "oid"]) {
      const a = (await call(c, "uuid.v5", { namespace, name: "rrotor" })).uuid as string;
      const b = (await call(c, "uuid.v5", { namespace, name: "rrotor" })).uuid as string;
      expect(a).toBe(b);
      expect(a[14]).toBe("5"); // version nibble
      expect("89ab").toContain(a[19]); // RFC variant bits
    }
  });

  it("refuses a namespace that is neither a UUID nor a well-known id", async () => {
    await expect(c.dispatch("uuid.v5", { namespace: "nope", name: "x" })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_MISSING_INPUT/) });
  });
});

describe("crypto pack — random.seeded", () => {
  const c = withPack();

  it("same seed → identical sequence; different seed → different sequence", async () => {
    const a = await call(c, "random.seeded", { seed: "alpha", count: 5 });
    const b = await call(c, "random.seeded", { seed: "alpha", count: 5 });
    const other = await call(c, "random.seeded", { seed: "beta", count: 5 });
    expect(a.values).toEqual(b.values);
    expect(a.values).not.toEqual(other.values);
    expect(a.count).toBe(5);
    for (const v of a.values as number[]) {
      expect(v).toBeGreaterThanOrEqual(0);
      expect(v).toBeLessThan(1);
    }
  });

  it("integers:true stays inside [min, max] inclusive", async () => {
    const r = await call(c, "random.seeded", { seed: "dice", count: 50, min: 1, max: 6, integers: true });
    for (const v of r.values as number[]) {
      expect(Number.isInteger(v)).toBe(true);
      expect(v).toBeGreaterThanOrEqual(1);
      expect(v).toBeLessThanOrEqual(6);
    }
  });

  it("caps count at the bound and reports truncated", async () => {
    const r = await call(c, "random.seeded", { seed: "big", count: 5000 });
    expect(r.count).toBe(1000);
    expect((r.values as number[]).length).toBe(1000);
    expect(r.truncated).toBe(true);
  });

  it("refuses a missing seed and an inverted range", async () => {
    await expect(c.dispatch("random.seeded", {})).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_MISSING_INPUT/) });
    await expect(c.dispatch("random.seeded", { seed: "s", min: 5, max: 1 })).resolves.toMatchObject({ ok: false });
  });
});

describe("crypto pack — jwt.decode", () => {
  const c = withPack();
  const seg = (o: unknown) => Buffer.from(JSON.stringify(o)).toString("base64url");

  it("decodes header + payload without verifying, and says so", async () => {
    const token = `${seg({ alg: "HS256", typ: "JWT" })}.${seg({ sub: "u-1", name: "chris" })}.fakesig`;
    const r = await call(c, "jwt.decode", { token });
    expect(r.header).toEqual({ alg: "HS256", typ: "JWT" });
    expect(r.payload).toEqual({ sub: "u-1", name: "chris" });
    expect(r.signature_b64).toBe("fakesig");
    expect(r.verified).toBe(false);
  });

  it("refuses a non-3-segment token and non-JSON segments", async () => {
    await expect(c.dispatch("jwt.decode", { token: "abc" })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_MISSING_INPUT/) });
    await expect(c.dispatch("jwt.decode", { token: "!!.??.sig" })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_MISSING_INPUT/) });
  });
});

describe("crypto pack — contract", () => {
  it("every tool is pure with no grants (installable in every permission mode)", () => {
    const pack = cryptoPack();
    expect(pack.name).toBe("crypto");
    expect(pack.version).toBe("1.0.0");
    expect(pack.tools).toHaveLength(7);
    for (const t of pack.tools) {
      expect(t.effect).toBe("pure");
      expect(t.grants).toEqual([]);
      expect((t.input as { required?: string[] }).required?.length).toBeGreaterThan(0);
    }
  });
});
