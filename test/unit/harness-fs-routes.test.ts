/**
 * The harness pod's workspace FS API: path CONTAINMENT (relative/absolute/
 * dot-dot/symlink escapes all refused), owner+thread workspace keying that
 * matches resolveRunConfig exactly (the panels browse where runs execute),
 * desktop-identical response shapes, the auth rules (principal required),
 * and the cursor-polled change events.
 */

import { describe, it, expect, afterEach } from "vitest";
import * as http from "node:http";
import type { AddressInfo } from "node:net";
import { mkdtempSync, mkdirSync, writeFileSync, symlinkSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { containPath, handleFsRequest, closeAllFsWatchers } from "../../src/harness/fs-routes.js";
import type { FsAuthContext } from "../../src/harness/fs-routes.js";
import { resolveRunConfig, sessionWorkspace, workspaceSegment } from "../../src/harness/config.js";

const HOME = mkdtempSync(join(tmpdir(), "fs-routes-"));

const OWNER: FsAuthContext = { enabled: true, principal: { orgId: "org-1", userId: "user-1" } };

const servers: http.Server[] = [];
afterEach(async () => {
  closeAllFsWatchers();
  for (const s of servers.splice(0)) await new Promise<void>((r) => s.close(() => r()));
});

function boot(authn: FsAuthContext, env: NodeJS.ProcessEnv = { HARNESS_HOME: HOME }): Promise<string> {
  const server = http.createServer((req, res) => {
    const path = (req.url ?? "/").split("?", 1)[0];
    handleFsRequest(authn, req, res, req.method ?? "GET", path, env);
  });
  servers.push(server);
  return new Promise((r) => {
    server.listen(0, "127.0.0.1", () => r(`http://127.0.0.1:${(server.address() as AddressInfo).port}`));
  });
}

async function post(base: string, path: string, body: Record<string, unknown>): Promise<{ status: number; json: Record<string, unknown> }> {
  const res = await fetch(`${base}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return { status: res.status, json: (await res.json()) as Record<string, unknown> };
}

describe("containPath", () => {
  const root = realpathSync(mkdtempSync(join(tmpdir(), "contain-")));

  it("admits the root, relative children, and absolute in-root paths", async () => {
    expect(await containPath(root, root)).toBe(root);
    expect(await containPath(root, "a/b.txt")).toBe(join(root, "a", "b.txt"));
    expect(await containPath(root, join(root, "x.txt"))).toBe(join(root, "x.txt"));
  });

  it("refuses dot-dot, absolute escapes, and junk", async () => {
    expect(await containPath(root, "../outside.txt")).toBeNull();
    expect(await containPath(root, join(root, "..", "outside.txt"))).toBeNull();
    expect(await containPath(root, "a/../../etc/passwd")).toBeNull();
    expect(await containPath(root, "/etc/passwd")).toBeNull();
    expect(await containPath(root, "")).toBeNull();
    expect(await containPath(root, 42)).toBeNull();
    expect(await containPath(root, "a\0b")).toBeNull();
  });

  it("refuses a symlink that hops out of the root", async () => {
    const outside = mkdtempSync(join(tmpdir(), "outside-"));
    writeFileSync(join(outside, "secret.txt"), "secret");
    symlinkSync(outside, join(root, "hop"));
    expect(await containPath(root, "hop/secret.txt")).toBeNull();
    // …while a symlink WITHIN the root stays fine.
    mkdirSync(join(root, "real"), { recursive: true });
    symlinkSync(join(root, "real"), join(root, "alias"));
    expect(await containPath(root, "alias")).not.toBeNull();
  });
});

describe("fs routes — auth + scope", () => {
  it("rejects a caller with no principal (the stator rule) and an auth-off pod", async () => {
    const noPrincipal = await boot({ enabled: true });
    expect((await post(noPrincipal, "/fs/info", { scope: "c123" })).status).toBe(403);
    const authOff = await boot({ enabled: false });
    expect((await post(authOff, "/fs/info", { scope: "c123" })).status).toBe(403);
  });

  it("requires a valid scope on a shared pod", async () => {
    const base = await boot(OWNER);
    expect((await post(base, "/fs/list", {})).status).toBe(400);
    expect((await post(base, "/fs/list", { scope: "../evil" })).status).toBe(400);
    expect((await post(base, "/fs/list", { scope: "no spaces" })).status).toBe(400);
  });

  it("keys the workspace by owner+thread — EXACTLY resolveRunConfig's key", async () => {
    const env = {
      HARNESS_HOME: HOME,
      GLYPHH_GATEWAY_URL: "https://gw.test",
      GLYPHH_RUNTIME_TOKEN: "tok",
    } as NodeJS.ProcessEnv;
    const base = await boot(OWNER, env);
    const { json } = await post(base, "/fs/info", { scope: "cthread1" });
    const cfg = resolveRunConfig("run-1", { prompt: "hi", threadId: "cthread1" }, env, { owner: "user-1" });
    expect(json.root).toBe(cfg.workdir);
    expect(cfg.workdir).toBe(sessionWorkspace(env, workspaceSegment({ owner: "user-1", threadId: "cthread1", runId: "run-1" })));
    // A DIFFERENT owner naming the same thread id lands in a DIFFERENT root.
    const other = await boot({ enabled: true, principal: { orgId: "org-1", userId: "user-2" } }, env);
    const otherRoot = (await post(other, "/fs/info", { scope: "cthread1" })).json.root;
    expect(otherRoot).not.toBe(json.root);
  });

  it("a token-bound session (dedicated pod) forces its own workspace", async () => {
    const env = { HARNESS_HOME: HOME } as NodeJS.ProcessEnv;
    const base = await boot({ ...OWNER, sessionId: "sess-9" }, env);
    const { json } = await post(base, "/fs/info", { scope: "cother" });
    expect(json.root).toBe(sessionWorkspace(env, "sess-9"));
  });
});

describe("fs routes — desktop-identical shapes + containment", () => {
  it("write → read → list → search round-trips with the desktop shapes", async () => {
    const base = await boot(OWNER);
    const w = await post(base, "/fs/write", { scope: "cshape", path: "src/hello.ts", content: "export const hi = 1;\n" });
    expect(w.json).toEqual({ ok: true });

    const info = (await post(base, "/fs/info", { scope: "cshape" })).json;
    const root = info.root as string;
    expect(info.isRepo).toBe(false);

    const r = (await post(base, "/fs/read", { scope: "cshape", path: join(root, "src", "hello.ts") })).json;
    expect(r).toEqual({ content: "export const hi = 1;\n" });

    const l = (await post(base, "/fs/list", { scope: "cshape" })).json as { entries: Array<{ name: string; path: string; kind: string }> };
    expect(l.entries).toEqual([{ name: "src", path: join(root, "src"), kind: "dir" }]);

    const s = (await post(base, "/fs/search", { scope: "cshape", query: "hello" })).json as { entries: Array<{ name: string; rel: string; path: string; kind: string }> };
    expect(s.entries).toEqual([{ name: "hello.ts", rel: join("src", "hello.ts"), path: join(root, "src", "hello.ts"), kind: "file" }]);

    const d = (await post(base, "/fs/diff", { scope: "cshape" })).json;
    expect(d).toEqual({ text: "", error: "not a git repo" });
  });

  it("refuses escapes IN the desktop error shapes (never leaks outside bytes)", async () => {
    const base = await boot(OWNER);
    const read = (await post(base, "/fs/read", { scope: "cesc", path: "/etc/passwd" })).json;
    expect(read.content).toBe("");
    expect(String(read.error)).toMatch(/outside/);
    const write = (await post(base, "/fs/write", { scope: "cesc", path: "../../evil.txt", content: "x" })).json;
    expect(write).toMatchObject({ ok: false });
    const list = (await post(base, "/fs/list", { scope: "cesc", dir: join(tmpdir()) })).json;
    expect(list).toMatchObject({ entries: [] });
    expect(String(list.error)).toMatch(/outside/);
  });

  it("caps a write at 2MB, in-shape", async () => {
    const base = await boot(OWNER);
    const big = "x".repeat(2_000_001);
    const w = (await post(base, "/fs/write", { scope: "cbig", path: "big.txt", content: big })).json;
    expect(w).toMatchObject({ ok: false });
    expect(String(w.error)).toMatch(/too large/);
  });
});

describe("fs routes — change events", () => {
  it("cursor-polls file-changed events after a write", async () => {
    const base = await boot(OWNER);
    const first = (await (await fetch(`${base}/fs/events?scope=cev`)).json()) as { seq: number; events: unknown[] };
    expect(first.events).toEqual([]); // cursor-less poll only establishes the cursor
    // The watcher debounces ~250ms and macOS fsevents delivery lags under
    // parallel test load — keep touching fresh files while polling so one
    // slow/dropped notification can't flake the assertion.
    const deadline = Date.now() + 8000;
    let events: Array<{ type: string; path?: string; seq: number }> = [];
    let touch = 0;
    let lastTouch = 0;
    while (Date.now() < deadline) {
      if (Date.now() - lastTouch > 400) {
        lastTouch = Date.now();
        await post(base, "/fs/write", { scope: "cev", path: `watched-${touch++}.txt`, content: "v1" });
      }
      const page = (await (await fetch(`${base}/fs/events?scope=cev&from=${first.seq}`)).json()) as { events: typeof events };
      events = page.events;
      if (events.some((e) => e.type === "file-changed" && /watched-\d+\.txt$/.test(e.path ?? ""))) break;
      await new Promise((r) => setTimeout(r, 50));
    }
    expect(events.some((e) => e.type === "file-changed" && /watched-\d+\.txt$/.test(e.path ?? ""))).toBe(true);
  });
});
