/**
 * The glyphh tool standard library: every pack exercised against a real temp
 * workspace + git repo, capability-gated install (permission modes), and the
 * headline property — an effectful tool step replays WITHOUT re-running the side
 * effect (determinism holds because tool outputs are checkpointed into the tape).
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { mkdtempSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import * as http from "node:http";
import type { AddressInfo } from "node:net";

import { chatPack } from "../../src/tools/index.js";

import { BasicConnections } from "../../src/plugins/connections.js";
import { buildBasicPlugins } from "../../src/plugins/index.js";
import { InProcessStore } from "../../src/exec/store.js";
import { execute } from "../../src/exec/executor.js";
import {
  buildStdlib,
  installStdlib,
  kvFromStore,
  fsPack,
  execPack,
  gitPack,
  docPack,
  coworkPack,
  MODES,
  defineTool,
  type KvLike,
} from "../../src/tools/index.js";
import type { RotorDocument } from "../../src/types.js";

let root: string;
beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "glyphh-tools-"));
});
afterAll(() => rmSync(root, { recursive: true, force: true }));

/** A connections plugin with a pack installed, plus a direct dispatch helper. */
function withPack(packTools: { tools: { name: string; handler: (a: Record<string, unknown>) => unknown }[] }) {
  const c = new BasicConnections();
  for (const t of packTools.tools) c.register(t.name, t.handler);
  return c;
}
const call = async (c: BasicConnections, name: string, args: Record<string, unknown>) => {
  const r = await c.dispatch(name, args);
  if (!r.ok) throw new Error(r.error);
  return r.result as Record<string, unknown>;
};

describe("fs pack", () => {
  it("writes, reads, edits, lists, globs and greps under the sandbox", async () => {
    const c = withPack(fsPack({ root }));
    await call(c, "file.write", { path: "src/a.ts", content: "export const x = 1;\nconst y = 2;\n" });
    await call(c, "file.write", { path: "src/b.ts", content: "export const z = 3;\n" });

    expect((await call(c, "file.read", { path: "src/a.ts" })).content).toContain("const y = 2");
    await call(c, "file.edit", { path: "src/a.ts", old: "const y = 2;", new: "const y = 42;" });
    expect((await call(c, "file.read", { path: "src/a.ts" })).content).toContain("const y = 42");

    const list = await call(c, "file.list", { path: "src" });
    expect((list.entries as { name: string }[]).map((e) => e.name)).toEqual(["a.ts", "b.ts"]);

    const glob = await call(c, "fs.glob", { pattern: "src/**/*.ts" });
    expect(glob.paths).toEqual(["src/a.ts", "src/b.ts"]);

    const grep = await call(c, "fs.grep", { pattern: "export const" });
    expect((grep.matches as { file: string }[]).map((m) => m.file).sort()).toEqual(["src/a.ts", "src/b.ts"]);
  });

  it("refuses a path that escapes the sandbox", async () => {
    const c = withPack(fsPack({ root }));
    await expect(c.dispatch("file.read", { path: "../../etc/passwd" })).resolves.toMatchObject({ ok: false });
    const r = await c.dispatch("file.write", { path: "../evil.txt", content: "x" });
    expect(r).toMatchObject({ ok: false, error: expect.stringMatching(/E_POLICY_DENIED/) });
  });
});

describe("exec pack", () => {
  it("runs a bash command and captures stdout + exit code", async () => {
    const c = withPack(execPack({ root }));
    const r = await call(c, "shell.bash", { command: "echo hello && exit 3" });
    expect(String(r.stdout).trim()).toBe("hello");
    expect(r.exit_code).toBe(3);
  });
});

describe("git pack", () => {
  let repo: string;
  beforeAll(() => {
    repo = mkdtempSync(join(tmpdir(), "glyphh-git-"));
    const g = (args: string[]) => execFileSync("git", args, { cwd: repo });
    g(["init", "-q"]);
    g(["config", "user.email", "t@t.dev"]);
    g(["config", "user.name", "T"]);
    writeFileSync(join(repo, "README.md"), "# hi\n");
  });
  afterAll(() => rmSync(repo, { recursive: true, force: true }));

  it("status → add → commit → log, structured", async () => {
    const c = withPack(gitPack({ root: repo }));
    const st = await call(c, "git.status", {});
    expect(st.clean).toBe(false);
    expect((st.files as { path: string }[]).some((f) => f.path === "README.md")).toBe(true);

    await call(c, "git.add", { path: "README.md" });
    await call(c, "git.commit", { message: "init" });

    const st2 = await call(c, "git.status", {});
    expect(st2.clean).toBe(true);

    const log = await call(c, "git.log", { limit: 5 });
    expect((log.commits as { subject: string }[])[0].subject).toBe("init");
    expect((await call(c, "git.branch", {})).branch).toBeTruthy();

    // diff of an unstaged change; show HEAD.
    writeFileSync(join(repo, "README.md"), "# hi\nmore\n");
    expect(String((await call(c, "git.diff", {})).diff)).toMatch(/\+more/);
    expect(String((await call(c, "git.show", { ref: "HEAD" })).content)).toMatch(/init/);
  });
});

describe("doc pack", () => {
  it("outlines a markdown doc and extracts a section", async () => {
    const c = withPack(docPack({ root }));
    await call(c, "doc.write", { path: "d.md", content: "# Title\nintro\n## A\nalpha body\n## B\nbeta body\n" });
    const o = await call(c, "doc.outline", { path: "d.md" });
    expect((o.outline as { title: string }[]).map((h) => h.title)).toEqual(["Title", "A", "B"]);
    const sec = await call(c, "doc.section", { path: "d.md", heading: "A" });
    expect(sec.text).toBe("alpha body");
    expect((await call(c, "doc.section", { path: "d.md", heading: "nope" })).found).toBe(false);
  });
});

describe("cowork pack", () => {
  it("persists todos and artifacts in kv", async () => {
    const mem = new Map<string, unknown>();
    const kv: KvLike = { get: async (k) => mem.get(k), set: async (k, v) => void mem.set(k, v) };
    const c = withPack(coworkPack(kv));
    await call(c, "todo.write", { todos: [{ task: "ship tools", status: "in_progress" }] });
    expect((await call(c, "todo.read", {})).todos).toEqual([{ task: "ship tools", status: "in_progress" }]);
    await call(c, "artifact.write", { name: "deck", content: "slide 1", kind: "markdown" });
    expect((await call(c, "artifact.read", { name: "deck" })).content).toBe("slide 1");
    expect((await call(c, "artifact.read", { name: "missing" })).found).toBe(false);
  });
});

describe("capability gating (permission modes)", () => {
  it("chat mode installs read tools but skips every mutating one", () => {
    const store = new InProcessStore();
    const plugins = buildBasicPlugins({ store });
    const c = new BasicConnections();
    const res = installStdlib(c, { root, memory: plugins.memory, kv: kvFromStore(store), granted: MODES.chat });
    expect(res.installed).toContain("file.read");
    expect(res.installed).toContain("git.status");
    // Mutating / exec tools are absent — not merely denied at call time, they never exist.
    for (const t of ["file.write", "file.edit", "shell.bash", "git.commit", "doc.write", "todo.write"]) {
      expect(res.installed).not.toContain(t);
      expect(res.skipped.map((s) => s.name)).toContain(t);
    }
    expect(c.listTools().map((t) => t.name)).not.toContain("shell.bash");
  });

  it("code mode installs the full workbench", () => {
    const store = new InProcessStore();
    const plugins = buildBasicPlugins({ store });
    const c = new BasicConnections();
    const res = installStdlib(c, { root, memory: plugins.memory, kv: kvFromStore(store), mode: "code" });
    for (const t of ["file.write", "shell.bash", "git.commit", "doc.write", "todo.write", "web.fetch"]) {
      expect(res.installed).toContain(t);
    }
    expect(res.skipped).toHaveLength(0);
  });
});

describe("determinism — an effectful tool step replays without re-running", () => {
  const doc = (): RotorDocument =>
    ({
      apiVersion: "rotor.glyphh.ai/v0.1",
      kind: "Rotor",
      metadata: { name: "writer", version: "0.1.0" },
      spec: {
        entry: "w",
        steps: [
          { id: "w", type: "tool", in: {}, out: {}, idempotency: "auto", config: { flavor: "mcp", name: "file.write", args: { path: "out.txt", content: "hello" } }, next: "end" },
        ],
      },
    }) as unknown as RotorDocument;

  it("writes on the fresh run, and replay does NOT re-dispatch the write", async () => {
    const store = new InProcessStore();
    const out = join(root, "out.txt");
    rmSync(out, { force: true });

    // Fresh run: the tool actually writes the file.
    const p1 = buildBasicPlugins({ store });
    installStdlib(p1.connections, { root, memory: p1.memory, kv: kvFromStore(store), mode: "code" });
    await execute(doc(), {}, p1);
    expect(existsSync(out)).toBe(true);

    // Delete it, then replay the SAME run (same doc + inputs + store → same run id).
    rmSync(out, { force: true });
    const p2 = buildBasicPlugins({ store });
    installStdlib(p2.connections, { root, memory: p2.memory, kv: kvFromStore(store), mode: "code" });
    await execute(doc(), {}, p2);

    // Replay returned the recorded output and never called file.write again.
    expect(existsSync(out)).toBe(false);
  });
});

describe("chat pack — web.fetch", () => {
  let server: http.Server;
  let url: string;
  beforeAll(async () => {
    server = http.createServer((_req, res) => {
      res.writeHead(200, { "content-type": "text/plain" });
      res.end("hello from the web");
    });
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
    url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/`;
  });
  afterAll(() => new Promise<void>((r) => server.close(() => r())));

  it("fetches a URL body and refuses a non-http url", async () => {
    const store = new InProcessStore();
    const plugins = buildBasicPlugins({ store });
    const c = withPack(chatPack({ memory: plugins.memory }));
    const r = await call(c, "web.fetch", { url });
    expect(r.status).toBe(200);
    expect(r.body).toBe("hello from the web");
    await expect(c.dispatch("web.fetch", { url: "ftp://x" })).resolves.toMatchObject({ ok: false });
  });
});

describe("tool error + edge paths", () => {
  it("fs edge cases: bad regex, missing edit target, ambiguous edit", async () => {
    const c = withPack(fsPack({ root }));
    await call(c, "file.write", { path: "dup.txt", content: "a a a" });
    await expect(c.dispatch("fs.grep", { pattern: "(" })).resolves.toMatchObject({ ok: false });
    await expect(c.dispatch("file.edit", { path: "dup.txt", old: "zzz", new: "y" })).resolves.toMatchObject({ ok: false });
    await expect(c.dispatch("file.edit", { path: "dup.txt", old: "a", new: "b" })).resolves.toMatchObject({ ok: false }); // 3× → ambiguous
    const ok = await call(c, "file.edit", { path: "dup.txt", old: "a", new: "b", all: true });
    expect(ok.replaced).toBe(3);
    // grep with a glob filter + ignore_case.
    await call(c, "file.write", { path: "g.md", content: "HELLO world" });
    const g = await call(c, "fs.grep", { pattern: "hello", glob: "*.md", ignore_case: true });
    expect((g.matches as unknown[]).length).toBe(1);
  });

  it("exec + cowork missing-input errors", async () => {
    const ce = withPack(execPack({ root }));
    await expect(ce.dispatch("shell.bash", { command: "  " })).resolves.toMatchObject({ ok: false });
    const kv: KvLike = { get: async () => undefined, set: async () => {} };
    const cc = withPack(coworkPack(kv));
    await expect(cc.dispatch("todo.write", { todos: "not-an-array" })).resolves.toMatchObject({ ok: false });
    await expect(cc.dispatch("artifact.write", { name: "", content: "x" })).resolves.toMatchObject({ ok: false });
  });
});

describe("wired into a live run via buildBasicPlugins({ tools })", () => {
  const writeDoc = (mode: string, path: string): RotorDocument =>
    ({
      apiVersion: "rotor.glyphh.ai/v0.1",
      kind: "Rotor",
      metadata: { name: "w", version: "0.1.0", labels: { mode } },
      spec: {
        entry: "w",
        steps: [{ id: "w", type: "tool", in: {}, out: {}, idempotency: "auto", config: { flavor: "mcp", name: "file.write", args: { path, content: "wired" } }, next: "end" }],
      },
    }) as unknown as RotorDocument;

  it("code mode: the rotor's file.write actually lands a file", async () => {
    const store = new InProcessStore();
    rmSync(join(root, "wired.txt"), { force: true });
    const plugins = buildBasicPlugins({ store, tools: { root, mode: "code" } });
    const r = await execute(writeDoc("code", "wired.txt"), {}, plugins);
    expect(r.status).toBe("ok");
    expect(existsSync(join(root, "wired.txt"))).toBe(true);
  });

  it("chat mode: file.write is not installed, so the step fails and nothing is written", async () => {
    const store = new InProcessStore();
    rmSync(join(root, "wired2.txt"), { force: true });
    const plugins = buildBasicPlugins({ store, tools: { root, mode: "chat" } });
    const r = await execute(writeDoc("chat", "wired2.txt"), {}, plugins);
    expect(r.status).toBe("failed"); // E_NO_TOOL — the tool doesn't exist in chat mode
    expect(existsSync(join(root, "wired2.txt"))).toBe(false);
  });
});

describe("SDK surface — defineTool", () => {
  it("a user-defined tool installs and dispatches behind the same contract", async () => {
    const shout = defineTool({
      name: "text.shout",
      version: 1,
      description: "Uppercase a string.",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { text: { type: "string" } }, required: ["text"] },
      handler: async (args) => ({ shouted: String(args.text ?? "").toUpperCase() }),
    });
    const c = new BasicConnections();
    c.register(shout.name, shout.handler);
    expect(await call(c, "text.shout", { text: "hi" })).toEqual({ shouted: "HI" });
  });
});
