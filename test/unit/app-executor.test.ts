/**
 * app-executor.test.ts — the headless app-worker runtime.
 *
 * The contract: an app's worker.js runs in a worker_thread whose ONLY global
 * surface is the desktop-mirrored `glyphh` bridge ({ call, handle, app }) plus
 * ECMAScript intrinsics — no process, no require, no timers, no Node globals.
 * Handlers register at load, invokes round-trip JSON, `glyphh.call` forwards
 * to the injected capability bridge, and a crashed / wedged / stale worker is
 * disposed so the next invoke restarts it fresh.
 */

import { describe, it, expect, afterEach } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Worker } from "node:worker_threads";

import { AppWorkerExecutor, stubCapabilityBridge, READY_TIMEOUT_MS, INVOKE_TIMEOUT_MS } from "../../src/app-worker/executor.js";
import type { ResolvedApp } from "../../src/app-worker/bundle.js";
import { appManifest } from "../harness/app-fixtures.js";

/** Write a materialized app dir (what AppBundleCache.resolveApp yields) with
 *  the given worker source, and return a stub resolveApp for it. */
function materializedApp(workerSource: string, sha = "cafe".repeat(16)): { app: ResolvedApp; resolveApp: () => Promise<ResolvedApp> } {
  const dir = mkdtempSync(join(tmpdir(), "app-exec-"));
  mkdirSync(dir, { recursive: true });
  const manifest = appManifest({ slug: "demo", worker: "worker.js" });
  writeFileSync(join(dir, "manifest.json"), JSON.stringify(manifest));
  writeFileSync(join(dir, "index.html"), "<html>");
  writeFileSync(join(dir, "worker.js"), workerSource);
  const app: ResolvedApp = { dir, manifest, sha256: sha };
  return { app, resolveApp: () => Promise.resolve(app) };
}

const executors: AppWorkerExecutor[] = [];
function makeExecutor(opts: ConstructorParameters<typeof AppWorkerExecutor>[0]): AppWorkerExecutor {
  const e = new AppWorkerExecutor(opts);
  executors.push(e);
  return e;
}

afterEach(async () => {
  await Promise.all(executors.splice(0).map((e) => e.disposeAll()));
});

describe("AppWorkerExecutor", () => {
  it("registers handlers at load and round-trips an invoke", async () => {
    const { resolveApp } = materializedApp(`
      glyphh.handle("echo", (args) => ({ got: args, from: glyphh.app.slug }));
    `);
    const exec = makeExecutor({ resolveApp });
    const result = await exec.invokeHandler("demo", "echo", { n: 42 });
    expect(result).toEqual({ got: { n: 42 }, from: "demo" });
  });

  it("supports async handlers and null results", async () => {
    const { resolveApp } = materializedApp(`
      glyphh.handle("later", async () => undefined);
    `);
    const exec = makeExecutor({ resolveApp });
    expect(await exec.invokeHandler("demo", "later")).toBeNull();
  });

  it("rejects an unknown handler by name", async () => {
    const { resolveApp } = materializedApp(`glyphh.handle("known", () => 1);`);
    const exec = makeExecutor({ resolveApp });
    await expect(exec.invokeHandler("demo", "unknown")).rejects.toThrow(/no handler registered: unknown/);
  });

  it("surfaces handler errors as rejections, worker stays up", async () => {
    const { resolveApp } = materializedApp(`
      glyphh.handle("boom", () => { throw new Error("kapow"); });
      glyphh.handle("ok", () => "fine");
    `);
    const exec = makeExecutor({ resolveApp });
    await expect(exec.invokeHandler("demo", "boom")).rejects.toThrow(/kapow/);
    expect(await exec.invokeHandler("demo", "ok")).toBe("fine");
  });

  // ── isolation ────────────────────────────────────────────────────────────

  it("exposes ONLY glyphh: no process/require/Buffer/timers/fetch/console", async () => {
    const { resolveApp } = materializedApp(`
      glyphh.handle("globals", () => {
        const missing = {};
        for (const name of ["process", "require", "module", "Buffer", "setTimeout",
                            "setInterval", "fetch", "console", "queueMicrotask", "__dirname"]) {
          missing[name] = typeof globalThis[name];
        }
        return { missing, hasGlyphh: typeof glyphh === "object" };
      });
    `);
    const exec = makeExecutor({ resolveApp });
    const seen = (await exec.invokeHandler("demo", "globals")) as {
      missing: Record<string, string>;
      hasGlyphh: boolean;
    };
    expect(seen.hasGlyphh).toBe(true);
    for (const [name, type] of Object.entries(seen.missing)) {
      expect(`${name}:${type}`).toBe(`${name}:undefined`);
    }
  });

  it("a process.exit attempt throws instead of exiting anything", async () => {
    const { resolveApp } = materializedApp(`
      glyphh.handle("die", () => process.exit(1));
      glyphh.handle("alive", () => "still here");
    `);
    const exec = makeExecutor({ resolveApp });
    await expect(exec.invokeHandler("demo", "die")).rejects.toThrow(/process is not defined/);
    // The refusal is a throw in the handler, not a death: the worker keeps serving.
    expect(await exec.invokeHandler("demo", "alive")).toBe("still here");
  });

  it("require of node builtins from the worker script fails", async () => {
    const { resolveApp } = materializedApp(`
      const fs = require("node:fs");
      glyphh.handle("read", () => fs.readFileSync("/etc/passwd", "utf8"));
    `);
    const exec = makeExecutor({ resolveApp });
    await expect(exec.invokeHandler("demo", "read")).rejects.toThrow(/require is not defined/);
  });

  it("glyphh is frozen — the bridge cannot be replaced", async () => {
    const { resolveApp } = materializedApp(`
      glyphh.handle("tamper", () => {
        let overwrote = true;
        try { glyphh.call = () => "pwned"; } catch { overwrote = false; }
        try { glyphh.app.slug = "other"; } catch {}
        return { overwrote, callType: typeof glyphh.call, slug: glyphh.app.slug };
      });
    `);
    const exec = makeExecutor({ resolveApp });
    const r = (await exec.invokeHandler("demo", "tamper")) as { callType: string; slug: string };
    expect(r.callType).toBe("function");
    expect(r.slug).toBe("demo");
  });

  // ── glyphh.call / capability bridge ──────────────────────────────────────

  it("glyphh.call('ping') round-trips through the stub bridge", async () => {
    const { resolveApp } = materializedApp(`
      glyphh.handle("probe", async (args) => glyphh.call("ping", args));
    `);
    const exec = makeExecutor({ resolveApp });
    expect(await exec.invokeHandler("demo", "probe", { x: 1 })).toEqual({ pong: true, args: { x: 1 } });
  });

  it("every other capability refuses until slice 4 wires the bridge", async () => {
    const { resolveApp } = materializedApp(`
      glyphh.handle("query", async () => glyphh.call("db.exec", { sql: "select 1" }));
    `);
    const exec = makeExecutor({ resolveApp });
    await expect(exec.invokeHandler("demo", "query")).rejects.toThrow(/capability bridge not wired \(slice 4\)/);
  });

  it("an injected bridge receives (slug, method, args)", async () => {
    const calls: unknown[] = [];
    const { resolveApp } = materializedApp(`
      glyphh.handle("go", async () => glyphh.call("custom.method", { a: 1 }));
    `);
    const exec = makeExecutor({
      resolveApp,
      capabilityBridge: async (slug, method, args) => {
        calls.push([slug, method, args]);
        return "bridged";
      },
    });
    expect(await exec.invokeHandler("demo", "go")).toBe("bridged");
    expect(calls).toEqual([["demo", "custom.method", { a: 1 }]]);
  });

  // ── lifecycle ────────────────────────────────────────────────────────────

  it("times out a hung handler, disposes the worker, and restarts on next invoke", async () => {
    const { resolveApp } = materializedApp(`
      let starts = (globalThis.__starts = (globalThis.__starts || 0) + 1);
      glyphh.handle("hang", () => new Promise(() => {}));
      glyphh.handle("hello", () => "hi");
    `);
    const exec = makeExecutor({ resolveApp });
    await expect(exec.invokeHandler("demo", "hang", {}, 200)).rejects.toThrow(/timed out after 200ms/);
    // The wedged worker was disposed; a fresh one serves the next invoke.
    expect(await exec.invokeHandler("demo", "hello")).toBe("hi");
  });

  it("a crashed worker restarts on the next invoke", async () => {
    const { resolveApp } = materializedApp(`glyphh.handle("hello", () => "hi");`);
    const exec = makeExecutor({ resolveApp });
    expect(await exec.invokeHandler("demo", "hello")).toBe("hi");

    // Kill the thread out from under the executor — the crash path, not dispose().
    const handle = (exec as unknown as { workers: Map<string, { worker: Worker }> }).workers.get("demo")!;
    await handle.worker.terminate();

    expect(await exec.invokeHandler("demo", "hello")).toBe("hi");
  });

  it("a worker script that fails at load reports a start error", async () => {
    const { resolveApp } = materializedApp(`throw new Error("busted at load");`);
    const exec = makeExecutor({ resolveApp });
    await expect(exec.invokeHandler("demo", "any")).rejects.toThrow(/failed to start: busted at load/);
  });

  it("a worker that never becomes ready hits the readiness cap", async () => {
    const { resolveApp } = materializedApp(`for (;;) {} /* spin forever at load */`);
    const exec = makeExecutor({ resolveApp, readyTimeoutMs: 300 });
    await expect(exec.invokeHandler("demo", "any")).rejects.toThrow(/did not become ready in 0.3s/);
  });

  it("a new bundle hash retires the old worker (one worker per slug+sha)", async () => {
    const first = materializedApp(`glyphh.handle("which", () => "v1");`, "a".repeat(64));
    const second = materializedApp(`glyphh.handle("which", () => "v2");`, "b".repeat(64));
    let current = first.app;
    const exec = makeExecutor({ resolveApp: () => Promise.resolve(current) });
    expect(await exec.invokeHandler("demo", "which")).toBe("v1");
    current = second.app;
    expect(await exec.invokeHandler("demo", "which")).toBe("v2");
  });

  it("refuses an app whose manifest declares no worker", async () => {
    const { app } = materializedApp(`/* unused */`);
    const noWorker: ResolvedApp = { ...app, manifest: { ...app.manifest, worker: undefined } };
    const exec = makeExecutor({ resolveApp: () => Promise.resolve(noWorker) });
    await expect(exec.invokeHandler("demo", "any")).rejects.toThrow(/declares no worker/);
  });

  it("dispose(slug) stops the worker; the next invoke restarts it", async () => {
    const { resolveApp } = materializedApp(`glyphh.handle("hello", () => "hi");`);
    const exec = makeExecutor({ resolveApp });
    expect(await exec.invokeHandler("demo", "hello")).toBe("hi");
    await exec.dispose("demo");
    expect(await exec.invokeHandler("demo", "hello")).toBe("hi");
  });

  it("mirrors the desktop's default timings", () => {
    expect(INVOKE_TIMEOUT_MS).toBe(60_000);
    expect(READY_TIMEOUT_MS).toBe(10_000);
    expect(typeof stubCapabilityBridge()).toBe("function");
  });
});
