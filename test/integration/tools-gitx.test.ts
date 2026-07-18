/**
 * `gitx` pack — exercised against real temp git repos: branch/undo surface
 * (checkout, restore, stash, rm, mv), metadata reads (tag, remote, blame), and
 * the network trio (clone/pull/push) against a LOCAL bare repo — no network.
 * Sandbox escapes and option-shaped args must refuse with E_POLICY_DENIED.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { mkdtempSync, rmSync, writeFileSync, existsSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFileSync } from "node:child_process";

import { gitxPack } from "../../src/tools/gitx.js";
import { BasicConnections } from "../../src/plugins/connections.js";

const cleanups: string[] = [];
afterAll(() => {
  for (const d of cleanups) rmSync(d, { recursive: true, force: true });
});

function makeRepo(): string {
  const repo = mkdtempSync(join(tmpdir(), "gitx-"));
  cleanups.push(repo);
  const g = (args: string[]) => execFileSync("git", args, { cwd: repo });
  g(["init", "-q", "-b", "main"]);
  g(["config", "user.email", "t@t.dev"]);
  g(["config", "user.name", "T"]);
  writeFileSync(join(repo, "README.md"), "one\ntwo\nthree\n");
  g(["add", "."]);
  g(["commit", "-qm", "init"]);
  return repo;
}

function withPack(pack: { tools: { name: string; handler: (a: Record<string, unknown>) => unknown }[] }): BasicConnections {
  const c = new BasicConnections();
  for (const t of pack.tools) c.register(t.name, t.handler);
  return c;
}
const call = async (c: BasicConnections, name: string, args: Record<string, unknown>) => {
  const r = await c.dispatch(name, args);
  if (!r.ok) throw new Error(r.error);
  return r.result as Record<string, unknown>;
};

describe("gitx — branch + undo surface", () => {
  let repo: string;
  let c: BasicConnections;
  beforeAll(() => {
    repo = makeRepo();
    c = withPack(gitxPack({ root: repo }));
  });

  it("checkout creates and switches branches", async () => {
    const r = await call(c, "git.checkout", { ref: "feat", create: true });
    expect(r.ref).toBe("feat");
    expect(execFileSync("git", ["rev-parse", "--abbrev-ref", "HEAD"], { cwd: repo }).toString().trim()).toBe("feat");
    await call(c, "git.checkout", { ref: "main" });
    expect(execFileSync("git", ["rev-parse", "--abbrev-ref", "HEAD"], { cwd: repo }).toString().trim()).toBe("main");
  });

  it("checkout of a missing ref fails structurally", async () => {
    await expect(c.dispatch("git.checkout", { ref: "no-such-branch" })).resolves.toMatchObject({ ok: false });
  });

  it("restore discards a working-tree change; --staged unstages", async () => {
    writeFileSync(join(repo, "README.md"), "MUTATED\n");
    await call(c, "git.restore", { paths: ["README.md"] });
    expect(readFileSync(join(repo, "README.md"), "utf8")).toBe("one\ntwo\nthree\n");

    writeFileSync(join(repo, "README.md"), "STAGED\n");
    execFileSync("git", ["add", "README.md"], { cwd: repo });
    await call(c, "git.restore", { paths: ["README.md"], staged: true });
    const st = execFileSync("git", ["status", "--porcelain"], { cwd: repo }).toString();
    expect(st).toMatch(/^ M README\.md/m); // modified but no longer staged
    await call(c, "git.restore", { paths: ["README.md"] }); // back to clean
  });

  it("stash push (with message) / list / pop round-trips a change", async () => {
    writeFileSync(join(repo, "README.md"), "stash me\n");
    await call(c, "git.stash", { op: "push", message: "wip-gitx" });
    expect(readFileSync(join(repo, "README.md"), "utf8")).toBe("one\ntwo\nthree\n");
    expect(String((await call(c, "git.stash", { op: "list" })).output)).toContain("wip-gitx");
    await call(c, "git.stash", { op: "pop" });
    expect(readFileSync(join(repo, "README.md"), "utf8")).toBe("stash me\n");
    execFileSync("git", ["checkout", "--", "README.md"], { cwd: repo });
  });

  it("rm deletes from tree + index; rm --cached keeps the file on disk", async () => {
    writeFileSync(join(repo, "junk.txt"), "x\n");
    execFileSync("git", ["add", "junk.txt"], { cwd: repo });
    execFileSync("git", ["commit", "-qm", "junk"], { cwd: repo });
    await call(c, "git.rm", { paths: ["junk.txt"] });
    expect(existsSync(join(repo, "junk.txt"))).toBe(false);
    execFileSync("git", ["commit", "-qm", "rm junk"], { cwd: repo });

    writeFileSync(join(repo, "keep.txt"), "y\n");
    execFileSync("git", ["add", "keep.txt"], { cwd: repo });
    await call(c, "git.rm", { paths: ["keep.txt"], cached: true });
    expect(existsSync(join(repo, "keep.txt"))).toBe(true); // still on disk, just unstaged
    rmSync(join(repo, "keep.txt"));
  });

  it("mv renames a tracked file (staged)", async () => {
    await call(c, "git.mv", { from: "README.md", to: "READ2.md" });
    expect(existsSync(join(repo, "READ2.md"))).toBe(true);
    expect(existsSync(join(repo, "README.md"))).toBe(false);
    execFileSync("git", ["commit", "-qm", "rename"], { cwd: repo });
  });
});

describe("gitx — tags, remotes, blame", () => {
  let repo: string;
  let c: BasicConnections;
  beforeAll(() => {
    repo = makeRepo();
    c = withPack(gitxPack({ root: repo }));
  });

  it("tag create (annotated) then list", async () => {
    await call(c, "git.tag", { op: "create", name: "v1.0.0", message: "first" });
    await call(c, "git.tag", { op: "create", name: "v1.0.1" }); // lightweight
    const list = await call(c, "git.tag", { op: "list" });
    expect(list.tags).toEqual(["v1.0.0", "v1.0.1"]);
    expect(list.truncated).toBe(false);
  });

  it("remote lists structured fetch/push entries", async () => {
    execFileSync("git", ["remote", "add", "origin", "https://example.invalid/repo.git"], { cwd: repo });
    const r = await call(c, "git.remote", {});
    const remotes = r.remotes as Array<{ name: string; url: string; kind: string }>;
    expect(remotes).toHaveLength(2);
    expect(remotes[0]).toEqual({ name: "origin", url: "https://example.invalid/repo.git", kind: "fetch" });
    expect(remotes.map((x) => x.kind).sort()).toEqual(["fetch", "push"]);
  });

  it("blame returns structured, capped lines with author + range support", async () => {
    const full = await call(c, "git.blame", { path: "README.md" });
    const lines = full.lines as Array<{ line: number; commit: string; author: string; date: string; text: string }>;
    expect(lines).toHaveLength(3);
    expect(lines.map((l) => l.text)).toEqual(["one", "two", "three"]);
    expect(lines[0].author).toBe("T");
    expect(lines[0].commit).toMatch(/^[0-9a-f]{12}$/);
    expect(lines[0].date).toMatch(/^\d{4}-\d{2}-\d{2}T/);
    expect(full.truncated).toBe(false);

    const ranged = await call(c, "git.blame", { path: "README.md", from: 2, to: 3 });
    expect((ranged.lines as { text: string }[]).map((l) => l.text)).toEqual(["two", "three"]);
  });

  it("blame on an untracked path fails structurally", async () => {
    await expect(c.dispatch("git.blame", { path: "nope.txt" })).resolves.toMatchObject({ ok: false });
  });
});

describe("gitx — clone / push / pull against a local bare remote", () => {
  let repo: string;
  let bare: string;
  let workspace: string;
  let c: BasicConnections;
  beforeAll(() => {
    repo = makeRepo();
    bare = mkdtempSync(join(tmpdir(), "gitx-bare-"));
    workspace = mkdtempSync(join(tmpdir(), "gitx-ws-"));
    cleanups.push(bare, workspace);
    execFileSync("git", ["init", "-q", "--bare", "-b", "main", bare]);
    execFileSync("git", ["remote", "add", "origin", bare], { cwd: repo });
    c = withPack(gitxPack({ root: repo }));
  });

  it("push publishes to the remote with -u, then pull is already up to date", async () => {
    const push = await call(c, "git.push", { remote: "origin", ref: "main", set_upstream: true });
    expect(String(push.output)).toMatch(/main/);
    expect(execFileSync("git", ["log", "--oneline", "main"], { cwd: bare }).toString()).toMatch(/init/);
    const pull = await call(c, "git.pull", {});
    expect(String(pull.output).toLowerCase()).toContain("up to date");
  });

  it("clone lands a shallow copy inside the workspace sandbox", async () => {
    const cw = withPack(gitxPack({ root: workspace }));
    const r = await call(cw, "git.clone", { url: bare, dir: "cloned" });
    expect(r.dir).toBe("cloned");
    expect(existsSync(join(workspace, "cloned", ".git"))).toBe(true);
    expect(existsSync(join(workspace, "cloned", "README.md"))).toBe(true);
  });

  it("clone refuses a dir that escapes the workspace", async () => {
    const cw = withPack(gitxPack({ root: workspace }));
    const r = await cw.dispatch("git.clone", { url: bare, dir: "../evil" });
    expect(r).toMatchObject({ ok: false, error: expect.stringMatching(/E_POLICY_DENIED/) });
  });
});

describe("gitx — argument guards", () => {
  let repo: string;
  let c: BasicConnections;
  beforeAll(() => {
    repo = makeRepo();
    c = withPack(gitxPack({ root: repo }));
  });

  it("refuses option-shaped refs/names (would parse as git flags)", async () => {
    for (const [tool, args] of [
      ["git.checkout", { ref: "--force" }],
      ["git.tag", { op: "create", name: "-d" }],
      ["git.push", { remote: "--mirror" }],
    ] as const) {
      const r = await c.dispatch(tool, args as Record<string, unknown>);
      expect(r).toMatchObject({ ok: false, error: expect.stringMatching(/E_POLICY_DENIED/) });
    }
  });

  it("refuses paths that escape the sandbox", async () => {
    for (const args of [{ paths: ["../outside.txt"] }]) {
      expect(await c.dispatch("git.restore", args)).toMatchObject({ ok: false, error: expect.stringMatching(/E_POLICY_DENIED/) });
      expect(await c.dispatch("git.rm", args)).toMatchObject({ ok: false, error: expect.stringMatching(/E_POLICY_DENIED/) });
    }
    const mv = await c.dispatch("git.mv", { from: "README.md", to: "../out.md" });
    expect(mv).toMatchObject({ ok: false, error: expect.stringMatching(/E_POLICY_DENIED/) });
  });

  it("missing-input errors: empty paths, bad stash op, tag create without name", async () => {
    await expect(c.dispatch("git.restore", { paths: [] })).resolves.toMatchObject({ ok: false });
    await expect(c.dispatch("git.rm", { paths: "not-an-array" })).resolves.toMatchObject({ ok: false });
    await expect(c.dispatch("git.stash", { op: "drop" })).resolves.toMatchObject({ ok: false });
    await expect(c.dispatch("git.tag", { op: "create" })).resolves.toMatchObject({ ok: false });
    await expect(c.dispatch("git.checkout", { ref: "  " })).resolves.toMatchObject({ ok: false });
    await expect(c.dispatch("git.blame", { path: "" })).resolves.toMatchObject({ ok: false });
  });
});
