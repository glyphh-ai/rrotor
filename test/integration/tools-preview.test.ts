/**
 * `preview` pack — preview.open exercised through the injected platform/launcher
 * seam (no real browser ever opens), and serve.static/serve.stop live-tested on an
 * ephemeral port: spawn the detached server, fetch a file, verify the traversal
 * guard and per-port idempotency, then stop it and watch the port die.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import * as net from "node:net";
import * as http from "node:http";

import { BasicConnections } from "../../src/plugins/connections.js";
import { previewPack, openCommand } from "../../src/tools/preview.js";

let root: string;
beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "rrotor-preview-"));
});
afterAll(() => {
  // Belt over braces: if a test failed mid-flight, kill any leftover server.
  try {
    const st = JSON.parse(readFileSync(join(root, ".rrotor-serve.json"), "utf8")) as { pid: number };
    process.kill(st.pid);
  } catch {
    /* none running */
  }
  rmSync(root, { recursive: true, force: true });
});

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

const freePort = (): Promise<number> =>
  new Promise((res) => {
    const s = net.createServer();
    s.listen(0, "127.0.0.1", () => {
      const p = (s.address() as net.AddressInfo).port;
      s.close(() => res(p));
    });
  });

/** Raw GET so the request path reaches the server unnormalized (fetch would collapse `..`). */
const rawGet = (port: number, path: string): Promise<{ status: number; type: string; body: string }> =>
  new Promise((res, rej) => {
    const req = http.request({ host: "127.0.0.1", port, method: "GET", path }, (r) => {
      let body = "";
      r.on("data", (d: Buffer) => (body += d.toString("utf8")));
      r.on("end", () => res({ status: r.statusCode ?? 0, type: String(r.headers["content-type"] ?? ""), body }));
    });
    req.on("error", rej);
    req.end();
  });

const portClosed = async (port: number, timeoutMs = 3000): Promise<boolean> => {
  const deadline = Date.now() + timeoutMs;
  const open = (): Promise<boolean> =>
    new Promise((res) => {
      const s = net.connect({ port, host: "127.0.0.1" }, () => {
        s.destroy();
        res(true);
      });
      s.on("error", () => {
        s.destroy();
        res(false);
      });
    });
  while (Date.now() < deadline) {
    if (!(await open())) return true;
    await new Promise((r) => setTimeout(r, 50));
  }
  return false;
};

describe("preview.open — command table (pure seam)", () => {
  it("picks the right opener per platform", () => {
    expect(openCommand("darwin", "http://x/")).toEqual({ cmd: "open", args: ["http://x/"] });
    expect(openCommand("linux", "http://x/")).toEqual({ cmd: "xdg-open", args: ["http://x/"] });
    expect(openCommand("win32", "http://x/")).toEqual({ cmd: "cmd", args: ["/c", "start", "", "http://x/"] });
    expect(() => openCommand("aix", "http://x/")).toThrow(/unsupported platform/);
  });
});

describe("preview.open — refusals + resolution via the injected launcher", () => {
  const launched: Array<{ cmd: string; args: string[] }> = [];
  const pack = () =>
    previewPack({
      root,
      platform: "darwin",
      launch: async (cmd, args) => {
        launched.push({ cmd, args });
      },
    });

  it("refuses non-http(s) schemes with E_POLICY_DENIED and never launches", async () => {
    const c = withPack(pack());
    for (const target of ["javascript:alert(1)", "file:///etc/passwd", "ftp://evil.example/x", "data:text/html,<b>x</b>"]) {
      const r = await c.dispatch("preview.open", { target });
      expect(r).toMatchObject({ ok: false, error: expect.stringMatching(/E_POLICY_DENIED/) });
    }
    expect(launched).toHaveLength(0);
  });

  it("refuses a path that escapes the sandbox", async () => {
    const c = withPack(pack());
    const r = await c.dispatch("preview.open", { target: "../../etc/passwd" });
    expect(r).toMatchObject({ ok: false, error: expect.stringMatching(/E_POLICY_DENIED/) });
    expect(launched).toHaveLength(0);
  });

  it("rejects a missing target and a nonexistent workspace path", async () => {
    const c = withPack(pack());
    await expect(c.dispatch("preview.open", { target: "  " })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_MISSING_INPUT/) });
    await expect(c.dispatch("preview.open", {})).resolves.toMatchObject({ ok: false });
    await expect(c.dispatch("preview.open", { target: "no-such-file.html" })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_TOOL/) });
    expect(launched).toHaveLength(0);
  });

  it("opens a workspace path as file:// through the platform opener", async () => {
    writeFileSync(join(root, "index.html"), "<h1>hi</h1>");
    const c = withPack(pack());
    const r = await call(c, "preview.open", { target: "index.html" });
    expect(r.opened).toBe(true);
    expect(String(r.target)).toMatch(/^file:\/\/.*index\.html$/);
    expect(launched.pop()).toEqual({ cmd: "open", args: [r.target] });
  });

  it("passes an http url straight through (linux → xdg-open)", async () => {
    const seen: Array<{ cmd: string; args: string[] }> = [];
    const c = withPack(previewPack({ root, platform: "linux", launch: async (cmd, args) => void seen.push({ cmd, args }) }));
    const r = await call(c, "preview.open", { target: "https://example.com/app?tab=1" });
    expect(r).toEqual({ opened: true, target: "https://example.com/app?tab=1" });
    expect(seen).toEqual([{ cmd: "xdg-open", args: ["https://example.com/app?tab=1"] }]);
  });
});

describe("serve.static + serve.stop — live on an ephemeral port", () => {
  beforeAll(() => {
    mkdirSync(join(root, "site", "assets"), { recursive: true });
    writeFileSync(join(root, "site", "index.html"), "<html><body>preview works</body></html>");
    writeFileSync(join(root, "site", "assets", "app.js"), "console.log('ok');");
    writeFileSync(join(root, "secret.txt"), "TOP SECRET"); // outside the served dir
  });

  it("serves files with content types, guards traversal, reuses per port, and stops", async () => {
    const c = withPack(previewPack({ root }));
    const port = await freePort();

    const r1 = await call(c, "serve.static", { dir: "site", port });
    expect(r1.url).toBe(`http://127.0.0.1:${port}/`);
    expect(r1.port).toBe(port);
    expect(typeof r1.pid).toBe("number");
    expect(existsSync(join(root, ".rrotor-serve.json"))).toBe(true);

    const idx = await rawGet(port, "/");
    expect(idx.status).toBe(200);
    expect(idx.type).toContain("text/html");
    expect(idx.body).toContain("preview works");

    const js = await rawGet(port, "/assets/app.js");
    expect(js.status).toBe(200);
    expect(js.type).toContain("text/javascript");

    expect((await rawGet(port, "/nope.css")).status).toBe(404);

    // Traversal guard: %2e%2e survives URL normalization, decodes to "..", and
    // must NOT reach root/secret.txt outside the served dir.
    const evil = await rawGet(port, "/%2e%2e/secret.txt");
    expect(evil.status).not.toBe(200);
    expect(evil.body).not.toContain("TOP SECRET");

    // Idempotent per port: a second call returns the SAME live server.
    const r2 = await call(c, "serve.static", { dir: "site", port });
    expect(r2.pid).toBe(r1.pid);

    const stop = await call(c, "serve.stop", {});
    expect(stop).toEqual({ stopped: true, pid: r1.pid });
    expect(existsSync(join(root, ".rrotor-serve.json"))).toBe(false);
    expect(await portClosed(port)).toBe(true);

    // Stopping again is a no-op, never a throw.
    expect(await call(c, "serve.stop", {})).toEqual({ stopped: false, pid: null });
  });

  it("refuses a dir escaping the sandbox and rejects bad inputs", async () => {
    const c = withPack(previewPack({ root }));
    await expect(c.dispatch("serve.static", { dir: "../outside" })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_POLICY_DENIED/) });
    await expect(c.dispatch("serve.static", { dir: "no-such-dir" })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_TOOL/) });
    await expect(c.dispatch("serve.static", { dir: "site", port: 70000 })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_MISSING_INPUT/) });
    await expect(c.dispatch("serve.static", { dir: "site/index.html" })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_TOOL/) });
  });

  it("serve.stop with a stale state file (dead pid) reports stopped:false and cleans up", async () => {
    const stateFile = join(root, ".rrotor-serve.json");
    writeFileSync(stateFile, JSON.stringify({ pid: 999999999, port: 1, dir: root }));
    const c = withPack(previewPack({ root }));
    expect(await call(c, "serve.stop", {})).toEqual({ stopped: false, pid: null });
    expect(existsSync(stateFile)).toBe(false);
  });
});
