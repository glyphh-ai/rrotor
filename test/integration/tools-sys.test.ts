/**
 * `sys` pack — host introspection tools dispatched directly against a
 * BasicConnections registry. The headline properties under test: env values that
 * look like secrets NEVER leave (literal "<redacted>"), env.list exposes names
 * only, and sys.which refuses anything that is not a bare command name.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { BasicConnections } from "../../src/plugins/connections.js";
import { sysPack } from "../../src/tools/sys.js";

let root: string;
beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "glyphh-sys-"));
});
afterAll(() => rmSync(root, { recursive: true, force: true }));

function withPack() {
  const c = new BasicConnections();
  for (const t of sysPack().tools) c.register(t.name, t.handler);
  return c;
}
const call = async (c: BasicConnections, name: string, args: Record<string, unknown>) => {
  const r = await c.dispatch(name, args);
  if (!r.ok) throw new Error(r.error);
  return r.result as Record<string, unknown>;
};

describe("sys.info", () => {
  it("reports host facts with sane shapes", async () => {
    const c = withPack();
    const r = await call(c, "sys.info", {});
    expect(r.platform).toBe(process.platform);
    expect(r.arch).toBe(process.arch);
    expect(typeof r.release).toBe("string");
    expect(r.cpus).toBeGreaterThan(0);
    expect(r.total_mem_bytes).toBeGreaterThan(0);
    expect(typeof r.free_mem_bytes).toBe("number");
    expect(String(r.node)).toMatch(/^v\d+/);
    expect(typeof r.hostname).toBe("string");
  });
});

describe("env.get", () => {
  it("returns a plain value for a non-secret name", async () => {
    process.env.GLYPHH_SYS_TEST_PLAIN = "visible-value";
    const c = withPack();
    const r = await call(c, "env.get", { name: "GLYPHH_SYS_TEST_PLAIN" });
    expect(r).toEqual({ name: "GLYPHH_SYS_TEST_PLAIN", value: "visible-value", redacted: false });
    delete process.env.GLYPHH_SYS_TEST_PLAIN;
  });

  it("redacts values whose NAME matches the secret pattern", async () => {
    const c = withPack();
    const secrets = ["GLYPHH_TEST_API_KEY", "GLYPHH_TEST_TOKEN", "GLYPHH_TEST_PASSWORD", "GLYPHH_TEST_AUTH_THING"];
    for (const name of secrets) {
      process.env[name] = "super-sensitive";
      const r = await call(c, "env.get", { name });
      expect(r).toEqual({ name, value: "<redacted>", redacted: true });
      expect(JSON.stringify(r)).not.toContain("super-sensitive");
      delete process.env[name];
    }
  });

  it("returns value null for a missing variable", async () => {
    const c = withPack();
    const r = await call(c, "env.get", { name: "GLYPHH_SYS_TEST_DOES_NOT_EXIST_9F3A" });
    expect(r).toEqual({ name: "GLYPHH_SYS_TEST_DOES_NOT_EXIST_9F3A", value: null, redacted: false });
  });

  it("refuses a missing/empty name with E_MISSING_INPUT", async () => {
    const c = withPack();
    await expect(c.dispatch("env.get", {})).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_MISSING_INPUT/) });
    await expect(c.dispatch("env.get", { name: "" })).resolves.toMatchObject({ ok: false });
  });
});

describe("env.list", () => {
  it("returns sorted names only — never any value", async () => {
    process.env.GLYPHH_SYS_TEST_LISTED_SECRET_KEY = "must-not-leak-anywhere";
    const c = withPack();
    const r = await call(c, "env.list", {});
    const names = r.names as string[];
    expect(names).toContain("GLYPHH_SYS_TEST_LISTED_SECRET_KEY");
    expect([...names]).toEqual([...names].sort());
    expect(r.count).toBeGreaterThanOrEqual(names.length);
    expect(typeof r.truncated).toBe("boolean");
    expect(JSON.stringify(r)).not.toContain("must-not-leak-anywhere");
    delete process.env.GLYPHH_SYS_TEST_LISTED_SECRET_KEY;
  });
});

describe("sys.which", () => {
  it("finds a command that certainly exists (node)", async () => {
    const c = withPack();
    const r = await call(c, "sys.which", { command: "node" });
    expect(r.found).toBe(true);
    expect(String(r.path)).toContain("node");
  });

  it("returns found:false for a command that does not exist", async () => {
    const c = withPack();
    const r = await call(c, "sys.which", { command: "glyphh-definitely-not-a-cmd-4e7b" });
    expect(r).toEqual({ path: null, found: false });
  });

  it("refuses paths, spaces, and shell metacharacters with E_MISSING_INPUT", async () => {
    const c = withPack();
    for (const bad of ["/bin/ls", "a b", "ls;rm", "$(whoami)", "", "../node"]) {
      await expect(c.dispatch("sys.which", { command: bad })).resolves.toMatchObject({
        ok: false,
        error: expect.stringMatching(/E_MISSING_INPUT/),
      });
    }
    await expect(c.dispatch("sys.which", {})).resolves.toMatchObject({ ok: false });
  });
});
