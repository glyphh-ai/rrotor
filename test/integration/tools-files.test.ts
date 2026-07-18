/**
 * `files` pack — every tool dispatched directly against a mkdtemp workspace:
 * happy paths, sandbox escapes (E_POLICY_DENIED), bounded reads (byte caps →
 * truncated), and the archive security gate (a hand-crafted tar with a `..`
 * member must be refused BEFORE extraction). No network anywhere.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { mkdtempSync, rmSync, writeFileSync, mkdirSync, existsSync, readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { gzipSync } from "node:zlib";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { BasicConnections } from "../../src/plugins/connections.js";
import { filesPack } from "../../src/tools/files.js";

let root: string;
beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "glyphh-files-"));
});
afterAll(() => rmSync(root, { recursive: true, force: true }));

function withPack(pack: { tools: { name: string; handler: (a: Record<string, unknown>) => unknown }[] }) {
  const c = new BasicConnections();
  for (const t of pack.tools) c.register(t.name, t.handler);
  return c;
}
const call = async (c: BasicConnections, name: string, args: Record<string, unknown>) => {
  const r = await c.dispatch(name, args);
  if (!r.ok) throw new Error(r.error);
  return r.result as Record<string, unknown>;
};

/** A minimal ustar entry — lets the tests craft archives tar itself refuses to create. */
function tarEntry(name: string, content: Buffer): Buffer {
  const h = Buffer.alloc(512);
  h.write(name, 0, "utf8");
  h.write("0000644 ", 100);
  h.write("0000000 ", 108);
  h.write("0000000 ", 116);
  h.write(content.length.toString(8).padStart(11, "0") + " ", 124);
  h.write("00000000000 ", 136);
  h.write("        ", 148); // checksum field is spaces while summing
  h.write("0", 156);
  h.write("ustar", 257);
  h.write("00", 263);
  let sum = 0;
  for (const b of h) sum += b;
  h.write(sum.toString(8).padStart(6, "0") + "\0 ", 148);
  const body = Buffer.alloc(Math.ceil(content.length / 512) * 512);
  content.copy(body);
  return Buffer.concat([h, body]);
}
const craftTgz = (entries: Array<[string, string]>): Buffer =>
  gzipSync(Buffer.concat([...entries.map(([n, c]) => tarEntry(n, Buffer.from(c))), Buffer.alloc(1024)]));

describe("files pack — mutation tools", () => {
  it("file.append creates then appends, and refuses an escape", async () => {
    const c = withPack(filesPack({ root }));
    await call(c, "file.append", { path: "logs/a.log", content: "one\n" });
    const r = await call(c, "file.append", { path: "logs/a.log", content: "two\n" });
    expect(r.bytes_appended).toBe(4);
    expect(readFileSync(join(root, "logs/a.log"), "utf8")).toBe("one\ntwo\n");
    const bad = await c.dispatch("file.append", { path: "../out.log", content: "x" });
    expect(bad).toMatchObject({ ok: false, error: expect.stringMatching(/E_POLICY_DENIED/) });
  });

  it("file.delete removes files; non-empty dirs need recursive:true", async () => {
    const c = withPack(filesPack({ root }));
    writeFileSync(join(root, "del.txt"), "x");
    expect((await call(c, "file.delete", { path: "del.txt" })).deleted).toBe(true);
    mkdirSync(join(root, "deldir/sub"), { recursive: true });
    writeFileSync(join(root, "deldir/sub/f.txt"), "x");
    await expect(c.dispatch("file.delete", { path: "deldir" })).resolves.toMatchObject({ ok: false }); // non-empty, no recursive
    expect((await call(c, "file.delete", { path: "deldir", recursive: true })).deleted).toBe(true);
    expect(existsSync(join(root, "deldir"))).toBe(false);
  });

  it("file.move renames within the sandbox and refuses an escaping destination", async () => {
    const c = withPack(filesPack({ root }));
    writeFileSync(join(root, "mv-src.txt"), "moved");
    await call(c, "file.move", { from: "mv-src.txt", to: "moved/dst.txt" });
    expect(readFileSync(join(root, "moved/dst.txt"), "utf8")).toBe("moved");
    expect(existsSync(join(root, "mv-src.txt"))).toBe(false);
    const bad = await c.dispatch("file.move", { from: "moved/dst.txt", to: "../../stolen.txt" });
    expect(bad).toMatchObject({ ok: false, error: expect.stringMatching(/E_POLICY_DENIED/) });
  });

  it("file.copy duplicates a file and errors on a missing source", async () => {
    const c = withPack(filesPack({ root }));
    writeFileSync(join(root, "cp-src.txt"), "12345");
    const r = await call(c, "file.copy", { from: "cp-src.txt", to: "cp/dst.txt" });
    expect(r.bytes).toBe(5);
    expect(readFileSync(join(root, "cp/dst.txt"), "utf8")).toBe("12345");
    await expect(c.dispatch("file.copy", { from: "no-such.txt", to: "x.txt" })).resolves.toMatchObject({ ok: false });
  });

  it("file.mkdir is recursive and idempotent (created:false on repeat)", async () => {
    const c = withPack(filesPack({ root }));
    expect((await call(c, "file.mkdir", { path: "a/b/c" })).created).toBe(true);
    expect((await call(c, "file.mkdir", { path: "a/b/c" })).created).toBe(false);
    expect(existsSync(join(root, "a/b/c"))).toBe(true);
  });

  it("file.touch creates an empty file without clobbering existing content", async () => {
    const c = withPack(filesPack({ root }));
    await call(c, "file.touch", { path: "t/new.txt" });
    expect(readFileSync(join(root, "t/new.txt"), "utf8")).toBe("");
    writeFileSync(join(root, "t/new.txt"), "keep");
    await call(c, "file.touch", { path: "t/new.txt" });
    expect(readFileSync(join(root, "t/new.txt"), "utf8")).toBe("keep");
  });
});

describe("files pack — read tools", () => {
  const hundred = Array.from({ length: 100 }, (_, i) => `line-${i + 1}`).join("\n") + "\n";

  it("file.stat reports kind, size and mode; missing path errors", async () => {
    const c = withPack(filesPack({ root }));
    writeFileSync(join(root, "st.txt"), "abcd");
    const r = await call(c, "file.stat", { path: "st.txt" });
    expect(r.kind).toBe("file");
    expect(r.size).toBe(4);
    expect(String(r.mtime_iso)).toMatch(/^\d{4}-\d{2}-\d{2}T/);
    expect((await call(c, "file.stat", { path: "." })).kind).toBe("dir");
    await expect(c.dispatch("file.stat", { path: "ghost.txt" })).resolves.toMatchObject({ ok: false });
  });

  it("file.head returns the first N lines and flags a byte-capped read", async () => {
    const c = withPack(filesPack({ root }));
    writeFileSync(join(root, "h.txt"), hundred);
    const r = await call(c, "file.head", { path: "h.txt", lines: 5 });
    expect(r.text).toBe("line-1\nline-2\nline-3\nline-4\nline-5");
    expect(r.total_lines_read).toBe(5);
    // Tiny byte cap: asking for more lines than the cap can hold flags truncated.
    const tiny = withPack(filesPack({ root, maxBytes: 20 }));
    const t = await call(tiny, "file.head", { path: "h.txt", lines: 50 });
    expect(t.truncated).toBe(true);
    expect(String(t.text).length).toBeLessThanOrEqual(20);
  });

  it("file.tail returns the last N lines from the tail bytes only", async () => {
    const c = withPack(filesPack({ root }));
    writeFileSync(join(root, "tl.txt"), hundred);
    const r = await call(c, "file.tail", { path: "tl.txt", lines: 3 });
    expect(r.text).toBe("line-98\nline-99\nline-100");
    // A byte-capped tail still lands on whole lines from the end.
    const tiny = withPack(filesPack({ root, maxBytes: 40 }));
    const t = await call(tiny, "file.tail", { path: "tl.txt", lines: 2 });
    expect(t.text).toBe("line-99\nline-100");
  });

  it("file.lines returns a 1-indexed inclusive range and validates it", async () => {
    const c = withPack(filesPack({ root }));
    writeFileSync(join(root, "ln.txt"), hundred);
    const r = await call(c, "file.lines", { path: "ln.txt", from: 5, to: 7 });
    expect(r.text).toBe("line-5\nline-6\nline-7");
    expect(r.from).toBe(5);
    expect(r.to).toBe(7);
    await expect(c.dispatch("file.lines", { path: "ln.txt", from: 9, to: 2 })).resolves.toMatchObject({
      ok: false,
      error: expect.stringMatching(/E_MISSING_INPUT/),
    });
    await expect(c.dispatch("file.lines", { path: "ln.txt", from: 0, to: 2 })).resolves.toMatchObject({ ok: false });
  });

  it("file.checksum streams a sha256 (default) and rejects an unknown algo", async () => {
    const c = withPack(filesPack({ root }));
    writeFileSync(join(root, "ck.txt"), "checksum me");
    const r = await call(c, "file.checksum", { path: "ck.txt" });
    expect(r.algo).toBe("sha256");
    expect(r.bytes).toBe(11);
    expect(r.hex).toBe(createHash("sha256").update("checksum me").digest("hex"));
    const sha1 = await call(c, "file.checksum", { path: "ck.txt", algo: "sha1" });
    expect(sha1.hex).toBe(createHash("sha1").update("checksum me").digest("hex"));
    await expect(c.dispatch("file.checksum", { path: "ck.txt", algo: "crc32" })).resolves.toMatchObject({
      ok: false,
      error: expect.stringMatching(/E_MISSING_INPUT/),
    });
  });
});

describe("files pack — tree + du", () => {
  let ws: string;
  beforeAll(() => {
    ws = mkdtempSync(join(tmpdir(), "glyphh-files-tree-"));
    mkdirSync(join(ws, "src/deep/deeper"), { recursive: true });
    mkdirSync(join(ws, "node_modules/pkg"), { recursive: true });
    mkdirSync(join(ws, ".git"), { recursive: true });
    writeFileSync(join(ws, "src/a.ts"), "x".repeat(300));
    writeFileSync(join(ws, "src/deep/b.ts"), "y".repeat(100));
    writeFileSync(join(ws, "src/deep/deeper/c.ts"), "z".repeat(50));
    writeFileSync(join(ws, "node_modules/pkg/big.js"), "n".repeat(9000));
    writeFileSync(join(ws, "top.md"), "m".repeat(700));
  });
  afterAll(() => rmSync(ws, { recursive: true, force: true }));

  it("fs.tree renders ascii, skips .git/node_modules, and honors depth", async () => {
    const c = withPack(filesPack({ root: ws }));
    const r = await call(c, "fs.tree", {});
    expect(r.tree).toContain("├── src/");
    expect(r.tree).toContain("a.ts");
    expect(r.tree).toContain("deeper/");
    expect(r.tree).not.toContain("node_modules");
    expect(r.tree).not.toContain(".git");
    expect(r.dirs).toBe(3);
    expect(r.files).toBe(3); // c.ts sits at depth 4 — beyond the default 3
    expect(r.tree).not.toContain("c.ts");
    const shallow = await call(c, "fs.tree", { depth: 1 });
    expect(shallow.tree).not.toContain("deep/");
    await expect(c.dispatch("fs.tree", { path: "top.md" })).resolves.toMatchObject({ ok: false }); // not a directory
  });

  it("fs.du ranks the largest files and sums the walked total", async () => {
    const c = withPack(filesPack({ root: ws }));
    const r = await call(c, "fs.du", {});
    const entries = r.entries as Array<{ path: string; bytes: number }>;
    expect(entries[0]).toEqual({ path: "top.md", bytes: 700 }); // node_modules/big.js is skipped
    expect(entries[1]).toEqual({ path: "src/a.ts", bytes: 300 });
    expect(r.total_bytes).toBe(700 + 300 + 100 + 50);
    expect(r.truncated).toBe(false);
  });
});

describe("files pack — base64 I/O", () => {
  it("file.write_b64 + file.read_b64 round-trip binary bytes", async () => {
    const c = withPack(filesPack({ root }));
    const bytes = Buffer.from([0, 1, 2, 254, 255, 10, 13]);
    await call(c, "file.write_b64", { path: "bin/blob.bin", b64: bytes.toString("base64") });
    expect(readFileSync(join(root, "bin/blob.bin")).equals(bytes)).toBe(true);
    const r = await call(c, "file.read_b64", { path: "bin/blob.bin" });
    expect(r.b64).toBe(bytes.toString("base64"));
    expect(r.bytes).toBe(7);
    expect(r.truncated).toBe(false);
  });

  it("file.read_b64 caps at max_bytes; file.write_b64 rejects junk input", async () => {
    const c = withPack(filesPack({ root }));
    writeFileSync(join(root, "bin/big.bin"), Buffer.alloc(100, 7));
    const r = await call(c, "file.read_b64", { path: "bin/big.bin", max_bytes: 16 });
    expect(r.bytes).toBe(16);
    expect(r.truncated).toBe(true);
    expect(Buffer.from(String(r.b64), "base64").length).toBe(16);
    await expect(c.dispatch("file.write_b64", { path: "bin/x.bin", b64: "not base64!!!" })).resolves.toMatchObject({
      ok: false,
      error: expect.stringMatching(/E_MISSING_INPUT/),
    });
  });
});

describe("files pack — archives", () => {
  it("archive.tar → archive.untar round-trips workspace files", async () => {
    const c = withPack(filesPack({ root }));
    writeFileSync(join(root, "pack-a.txt"), "alpha");
    mkdirSync(join(root, "pkgdir"), { recursive: true });
    writeFileSync(join(root, "pkgdir/pack-b.txt"), "beta");
    const t = await call(c, "archive.tar", { paths: ["pack-a.txt", "pkgdir"], out: "out/bundle.tgz" });
    expect(t.files).toBe(2);
    expect(Number(t.bytes)).toBeGreaterThan(0);
    expect(existsSync(join(root, "out/bundle.tgz"))).toBe(true);

    const u = await call(c, "archive.untar", { path: "out/bundle.tgz", into: "restored" });
    expect(u.count).toBeGreaterThanOrEqual(2);
    expect((u.files as string[]).some((f) => f.includes("pack-a.txt"))).toBe(true);
    expect(readFileSync(join(root, "restored/pack-a.txt"), "utf8")).toBe("alpha");
    expect(readFileSync(join(root, "restored/pkgdir/pack-b.txt"), "utf8")).toBe("beta");
  });

  it("archive.tar refuses an input path outside the sandbox", async () => {
    const c = withPack(filesPack({ root }));
    writeFileSync(join(root, "ok.txt"), "x");
    const r = await c.dispatch("archive.tar", { paths: ["ok.txt", "../../etc"], out: "evil.tgz" });
    expect(r).toMatchObject({ ok: false, error: expect.stringMatching(/E_POLICY_DENIED/) });
    expect(existsSync(join(root, "evil.tgz"))).toBe(false); // refused before tar ever ran
    await expect(c.dispatch("archive.tar", { paths: [], out: "x.tgz" })).resolves.toMatchObject({ ok: false });
  });

  it("archive.untar refuses a crafted tar with a `..` traversal member", async () => {
    const c = withPack(filesPack({ root }));
    writeFileSync(join(root, "mal.tgz"), craftTgz([["../escape.txt", "evil"]]));
    const r = await c.dispatch("archive.untar", { path: "mal.tgz", into: "malout" });
    expect(r).toMatchObject({ ok: false, error: expect.stringMatching(/E_POLICY_DENIED/) });
    expect(existsSync(join(root, "..", "escape.txt"))).toBe(false);
    expect(existsSync(join(root, "malout/escape.txt"))).toBe(false); // nothing extracted at all
  });

  it("archive.untar refuses an absolute-path member", async () => {
    const c = withPack(filesPack({ root }));
    writeFileSync(join(root, "mal-abs.tgz"), craftTgz([["/tmp/abs-evil.txt", "evil"]]));
    const r = await c.dispatch("archive.untar", { path: "mal-abs.tgz", into: "absout" });
    expect(r).toMatchObject({ ok: false, error: expect.stringMatching(/E_POLICY_DENIED/) });
  });
});
