/**
 * executor.ts — the pod-side headless runtime for a workpanel app's worker.js.
 *
 * The desktop runs an app's worker in a hidden, sandboxed BrowserWindow whose
 * CSP leaves the script nothing but the `glyphh` bridge
 * (app/src/main/glyphh-app-worker.ts). A pod has no Electron, so this mirrors
 * that surface with a Node `worker_thread` + `node:vm` context:
 *
 *   ONLY global surface: `glyphh` = { call, handle, app: { slug } } —
 *   plus the ECMAScript intrinsics every JS realm has (Object, Promise, JSON…).
 *   There is NO process, require, module, import, Buffer, setTimeout, fetch,
 *   console, or any other Node global: touching them is a ReferenceError, so a
 *   `process.exit(…)` attempt THROWS instead of exiting anything.
 *
 * The bridge crosses the sandbox boundary as PRIMITIVES ONLY (JSON strings +
 * null-prototyped functions), so the app script cannot walk `.constructor` off
 * a host object into the runner realm. `node:vm` is still not a hardware
 * boundary — the pod's real walls are the container and the server-side
 * capability checks — but the worker's programmatic surface is exactly the
 * desktop's: the bridge, nothing else.
 *
 * Lifecycle mirrors the desktop's timings and rules: one worker per
 * (slug, sha256), started lazily on first invoke; readiness (top-level code
 * ran, sync handle() registrations in) capped at 10s; a handler invoke capped
 * at 60s by default. A worker that crashes, times out, or belongs to a stale
 * bundle hash is disposed — the NEXT invoke restarts it fresh.
 *
 * `glyphh.call` forwards to a dependency-injected capability bridge. The REAL
 * bridge lives in capability-bridge.ts (per-app worker-token clients, adapted
 * via `executorBridge`); the default stub answers `ping` (a liveness probe for
 * tests) and refuses everything else.
 */

import { join, resolve } from "node:path";
import { Worker } from "node:worker_threads";

import type { ResolvedApp } from "./bundle.js";

/** Desktop-mirrored timings (app/src/main/glyphh-app-worker.ts). */
export const INVOKE_TIMEOUT_MS = 60_000;
export const READY_TIMEOUT_MS = 10_000;

/** The host side of `glyphh.call(method, args)` — the real per-app bridge is
 *  capability-bridge.ts (`createCapabilityBridge` + `executorBridge`). */
export type CapabilityBridge = (slug: string, method: string, args: unknown) => Promise<unknown>;

/** The stand-in bridge tests keep: `ping` echoes (so a roundtrip is testable),
 *  every real capability refuses loudly. */
export function stubCapabilityBridge(): CapabilityBridge {
  return async (_slug, method, args) => {
    if (method === "ping") return { pong: true, args: args ?? null };
    throw new Error("capability bridge not wired (pass capabilityBridge — see capability-bridge.ts)");
  };
}

/**
 * The runner: trusted host-realm code executed IN the worker thread (via
 * `eval: true`, so it resolves identically under tsx, vitest, and dist).
 * It builds the vm context, evaluates the app's worker.js inside it, and
 * relays invoke/call messages. Everything handed to the sandbox is a JSON
 * string or a null-prototyped function; everything received back is a JSON
 * string or a sandbox-realm callback invoked with primitives.
 */
const RUNNER_SOURCE = `
"use strict";
const { parentPort, workerData } = require("node:worker_threads");
const { readFileSync } = require("node:fs");
const vm = require("node:vm");

let callSeq = 0;
const pendingCalls = new Map(); // callId -> { resolve(json), reject(message) } (sandbox callbacks)

// sandbox -> host capability call. Null prototype: no .constructor/.call/.apply
// to walk from the sandbox into the host realm.
function hostCall(method, argsJson, resolveCb, rejectCb) {
  const callId = ++callSeq;
  pendingCalls.set(callId, { resolve: resolveCb, reject: rejectCb });
  parentPort.postMessage({ type: "call", callId, method: String(method), argsJson: String(argsJson) });
}
Object.setPrototypeOf(hostCall, null);

const context = vm.createContext(Object.create(null), {
  codeGeneration: { strings: true, wasm: false },
});

// Node injects its own console into vm contexts — a HOST-realm object whose
// methods would let sandbox code walk .constructor into the runner realm.
// The worker surface is glyphh and nothing else: strip it.
vm.runInContext("delete globalThis.console;", context);

// Build the ENTIRE glyphh bridge inside the sandbox realm, so every object the
// app script can reach is sandbox-born. The host contributes only hostCall.
const bootstrap = \`(function (slug, hostCall) {
  "use strict";
  const handlers = new Map();
  const glyphh = Object.freeze({
    call(method, args) {
      return new Promise((resolve, reject) => {
        hostCall(
          String(method),
          JSON.stringify(args === undefined ? null : args),
          (json) => { try { resolve(JSON.parse(json)); } catch (e) { reject(e); } },
          (message) => reject(new Error(String(message)))
        );
      });
    },
    handle(name, fn) {
      if (typeof name !== "string" || !name.trim()) throw new TypeError("handler name is required");
      if (typeof fn !== "function") throw new TypeError("handler must be a function");
      handlers.set(name.trim(), fn);
    },
    app: Object.freeze({ slug: String(slug) }),
  });
  Object.defineProperty(globalThis, "glyphh", { value: glyphh, enumerable: true });
  return function invoke(name, argsJson) {
    const fn = handlers.get(String(name));
    if (!fn) return Promise.reject(new Error("no handler registered: " + name));
    return Promise.resolve()
      .then(() => fn(JSON.parse(argsJson)))
      .then((result) => JSON.stringify(result === undefined ? null : result));
  };
})\`;
const invoke = vm.runInContext(bootstrap, context, { filename: "glyphh-bootstrap.js" })(
  workerData.slug,
  hostCall
);

// Evaluate the app's worker script. After this returns, all top-level
// synchronous glyphh.handle() registrations are in -> report ready.
try {
  const source = readFileSync(workerData.scriptPath, "utf8");
  vm.runInContext(source, context, { filename: workerData.scriptFile });
} catch (err) {
  parentPort.postMessage({ type: "init-error", error: err && err.message ? String(err.message) : String(err) });
  throw err;
}
parentPort.postMessage({ type: "ready" });

parentPort.on("message", (msg) => {
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "invoke") {
    Promise.resolve()
      .then(() => invoke(msg.name, msg.argsJson))
      .then(
        (resultJson) => parentPort.postMessage({ type: "result", callId: msg.callId, ok: true, resultJson }),
        (err) => parentPort.postMessage({
          type: "result",
          callId: msg.callId,
          ok: false,
          error: err && err.message ? String(err.message) : String(err),
        })
      );
  } else if (msg.type === "call-result") {
    const p = pendingCalls.get(msg.callId);
    if (!p) return;
    pendingCalls.delete(msg.callId);
    if (msg.ok) p.resolve(String(msg.resultJson));
    else p.reject(String(msg.error || "capability call failed"));
  }
});
`;

interface PendingInvoke {
  resolve: (v: unknown) => void;
  reject: (e: Error) => void;
  timer: NodeJS.Timeout;
}

interface WorkerHandle {
  key: string; // slug@sha256 — a new bundle hash means a new worker
  slug: string;
  worker: Worker;
  ready: Promise<void>;
  pending: Map<number, PendingInvoke>;
  disposed: boolean;
}

export interface AppWorkerExecutorOptions {
  /** The bundle seam: slug → materialized dir + manifest + content hash
   *  (AppBundleCache.resolveApp, or a stub in tests). */
  resolveApp: (slug: string) => Promise<ResolvedApp>;
  /** Host side of glyphh.call — defaults to the slice-2 stub. */
  capabilityBridge?: CapabilityBridge;
  /** Test override for the 10s readiness cap. */
  readyTimeoutMs?: number;
}

/**
 * Runs app workers and invokes their registered handlers. One executor serves
 * many apps; workers are keyed by (slug, bundle hash) and started lazily.
 */
export class AppWorkerExecutor {
  private readonly workers = new Map<string, WorkerHandle>(); // slug → handle
  private readonly bridge: CapabilityBridge;
  private readonly readyTimeoutMs: number;
  private callSeq = 0;

  constructor(private readonly opts: AppWorkerExecutorOptions) {
    this.bridge = opts.capabilityBridge ?? stubCapabilityBridge();
    this.readyTimeoutMs = opts.readyTimeoutMs ?? READY_TIMEOUT_MS;
  }

  /**
   * Invoke a named handler in the app's worker (starting it if needed).
   * Rejects on: unknown handler, handler error, worker crash, or timeout —
   * a timed-out worker may be wedged in synchronous code, so it is disposed
   * and the next invoke gets a fresh one.
   */
  async invokeHandler(slug: string, name: string, args: unknown = {}, timeoutMs = INVOKE_TIMEOUT_MS): Promise<unknown> {
    const handle = await this.ensureWorker(slug);
    const callId = ++this.callSeq;
    return new Promise<unknown>((resolvePromise, rejectPromise) => {
      const timer = setTimeout(() => {
        handle.pending.delete(callId);
        // A stuck handler may be a synchronous spin — the thread is unusable.
        void this.disposeHandle(handle);
        rejectPromise(new Error(`handler "${name}" in "${slug}" timed out after ${timeoutMs}ms`));
      }, timeoutMs);
      handle.pending.set(callId, { resolve: resolvePromise, reject: rejectPromise, timer });
      handle.worker.postMessage({ type: "invoke", callId, name: String(name), argsJson: JSON.stringify(args ?? {}) });
    });
  }

  /** Stop the app's worker (if running). The next invoke restarts it. */
  async dispose(slug: string): Promise<void> {
    const handle = this.workers.get(slug);
    if (handle) await this.disposeHandle(handle);
  }

  /** Stop every worker — pod shutdown / test teardown. */
  async disposeAll(): Promise<void> {
    await Promise.all([...this.workers.values()].map((h) => this.disposeHandle(h)));
  }

  private async ensureWorker(slug: string): Promise<WorkerHandle> {
    const app = await this.opts.resolveApp(slug);
    if (!app.manifest.worker) throw new Error(`app "${slug}" declares no worker script`);
    const key = `${slug}@${app.sha256}`;

    const existing = this.workers.get(slug);
    if (existing && !existing.disposed) {
      if (existing.key === key) {
        await existing.ready;
        return existing;
      }
      // The bundle moved under the slug — retire the stale worker.
      await this.disposeHandle(existing);
    }

    const handle = this.startWorker(slug, key, app);
    this.workers.set(slug, handle);
    try {
      await handle.ready;
    } catch (err) {
      await this.disposeHandle(handle);
      throw err;
    }
    return handle;
  }

  private startWorker(slug: string, key: string, app: ResolvedApp): WorkerHandle {
    const scriptPath = resolve(join(app.dir, app.manifest.worker!));
    const worker = new Worker(RUNNER_SOURCE, {
      eval: true,
      workerData: { slug: app.manifest.slug, scriptPath, scriptFile: app.manifest.worker },
    });

    const pending = new Map<number, PendingInvoke>();
    let markReady!: () => void;
    let failReady!: (e: Error) => void;
    const ready = new Promise<void>((res, rej) => {
      markReady = res;
      failReady = rej;
    });
    ready.catch(() => undefined); // ensureWorker awaits it; don't double-report

    const readyTimer = setTimeout(() => {
      failReady(new Error(`worker for "${slug}" did not become ready in ${this.readyTimeoutMs / 1000}s`));
      void this.disposeHandle(handle);
    }, this.readyTimeoutMs);

    const handle: WorkerHandle = { key, slug, worker, ready, pending, disposed: false };

    worker.on("message", (msg: { type?: string; callId?: number; ok?: boolean; resultJson?: string; error?: string; method?: string; argsJson?: string }) => {
      if (!msg || typeof msg !== "object") return;
      if (msg.type === "ready") {
        clearTimeout(readyTimer);
        markReady();
      } else if (msg.type === "init-error") {
        clearTimeout(readyTimer);
        failReady(new Error(`worker for "${slug}" failed to start: ${msg.error}`));
      } else if (msg.type === "result" && typeof msg.callId === "number") {
        const p = pending.get(msg.callId);
        if (!p) return;
        pending.delete(msg.callId);
        clearTimeout(p.timer);
        if (msg.ok) {
          try {
            p.resolve(msg.resultJson === undefined ? null : JSON.parse(msg.resultJson));
          } catch (e) {
            p.reject(e as Error);
          }
        } else {
          p.reject(new Error(msg.error || "handler failed"));
        }
      } else if (msg.type === "call" && typeof msg.callId === "number") {
        // glyphh.call → the capability bridge; the answer travels back as JSON.
        let args: unknown = null;
        try {
          args = JSON.parse(msg.argsJson ?? "null");
        } catch {
          /* leave null */
        }
        this.bridge(slug, String(msg.method), args).then(
          (result) =>
            worker.postMessage({ type: "call-result", callId: msg.callId, ok: true, resultJson: JSON.stringify(result === undefined ? null : result) }),
          (err: unknown) =>
            worker.postMessage({ type: "call-result", callId: msg.callId, ok: false, error: err instanceof Error ? err.message : String(err) })
        );
      }
    });

    const drop = (why: string) => {
      clearTimeout(readyTimer);
      failReady(new Error(`worker for "${slug}" ${why}`));
      handle.disposed = true;
      if (this.workers.get(slug) === handle) this.workers.delete(slug);
      for (const [id, p] of pending) {
        pending.delete(id);
        clearTimeout(p.timer);
        p.reject(new Error(`worker for "${slug}" ${why}`));
      }
    };
    worker.on("error", (err) => drop(`crashed: ${err.message}`));
    worker.on("exit", () => drop("went away"));

    return handle;
  }

  private async disposeHandle(handle: WorkerHandle): Promise<void> {
    if (handle.disposed) return;
    handle.disposed = true;
    if (this.workers.get(handle.slug) === handle) this.workers.delete(handle.slug);
    for (const [id, p] of handle.pending) {
      handle.pending.delete(id);
      clearTimeout(p.timer);
      p.reject(new Error(`worker for "${handle.slug}" was disposed`));
    }
    await handle.worker.terminate().catch(() => undefined);
  }
}
