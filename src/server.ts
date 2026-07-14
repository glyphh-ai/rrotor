/**
 * server.ts — the HTTP face of a rotor runtime instance, for Kubernetes.
 *
 * A rotor runtime is a **stateless, fungible instance** (SPEC.md §17.1): no
 * run-critical state lives in this process — the Context, the event history of
 * `StepRecord`s, and the caches all live in the external stator backend, and the
 * gateway is the only transport boundary. That is exactly what makes a runtime a
 * horizontally-scaled Kubernetes Deployment of interchangeable pods (docs/
 * runtime.md §5.3): any pod can serve any request, and a pod that dies is
 * replaced while the run continues on its successor.
 *
 * This module is deliberately dependency-light — `node:http` only, no framework —
 * so the probe surface has no cold-start cost of its own. It exposes:
 *
 *   GET  /healthz   liveness  — the process is up and the event loop turns
 *   GET  /readyz    readiness — every advertised capability seam is ready
 *   GET  /version   build identity (the pinned runtime version)
 *   POST /run       accept a rotor + inputs; parse → validate → execute → return
 *
 * `startServer(port)` returns the listening `http.Server`. The POST /run path is
 * wired to the executor against the basic-tier plugin bundle.
 */

import * as http from "node:http";
import { fileURLToPath } from "node:url";

import { Runtime } from "./runtime/runtime.js";
import { VERSION } from "./version.js";
import { parseRotor, validateRotor } from "./parser/index.js";
import { execute } from "./exec/executor.js";
import { buildBasicPlugins } from "./plugins/index.js";
import { statorFromEnv, statorFromEnvAsync } from "./exec/stator.js";
import { drainFromEnv } from "./plugins/drain.js";
import { log } from "./obs/logger.js";
import { describe } from "./errors.js";
import { toolModeFromLabels } from "./tools/index.js";
import { streamRunLive, replayRun } from "./transport/sse.js";

/** The per-session workspace sandbox for fs/exec/git tools (docs/hosting.md §3). */
function workspaceRoot(): string {
  return process.env.ROTOR_WORKSPACE ?? process.cwd();
}
import type { Stator } from "./exec/store.js";
import type { DrainPlugin } from "./plugins/interfaces.js";
import type { CapabilityStatus } from "./runtime/registry.js";
import type { RotorDocument } from "./types.js";

/** Coerce the POST /run `rotor` field into a document: accept an inline object
 *  (already-parsed RotorDocument) or a YAML/JSON source string. */
function coerceRotor(rotor: unknown): RotorDocument {
  if (typeof rotor === "string") {
    const fmt = rotor.trimStart().startsWith("{") ? "json" : "yaml";
    return parseRotor(rotor, fmt);
  }
  return rotor as RotorDocument;
}

const DEFAULT_PORT = 8080;

/** A JSON body helper — one place that sets content-type + status. */
function sendJson(res: http.ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(payload),
    "cache-control": "no-store",
  });
  res.end(payload);
}

/**
 * Readiness derives from the runtime's capability manifest (docs/runtime.md §3.8):
 * the pod is READY only when every advertised seam reports `ready` — so the probe
 * tells the truth about what the pod can serve. The per-capability ready/tier is
 * returned so an operator can see exactly which seam is holding readiness down.
 * (Per-rotor capability gaps are handled at run time by clean-refusal, not by the
 * pod-level probe — see `Runtime.reconcile`.)
 */
export function computeReadiness(manifest: Record<string, CapabilityStatus>): {
  ready: boolean;
  capabilities: Record<string, { ready: boolean; tier: string }>;
} {
  const capabilities: Record<string, { ready: boolean; tier: string }> = {};
  for (const [name, st] of Object.entries(manifest)) {
    capabilities[name] = { ready: st.ready, tier: st.tier };
  }
  const names = Object.keys(capabilities);
  const ready = names.length > 0 && names.every((n) => capabilities[n].ready);
  return { ready, capabilities };
}

function readiness(rt: Runtime): ReturnType<typeof computeReadiness> {
  return computeReadiness(rt.status());
}

/** The request router. Kept flat and allocation-light — this is the probe path. */
function handle(
  rt: Runtime,
  store: Stator,
  drain: DrainPlugin,
  req: http.IncomingMessage,
  res: http.ServerResponse,
): void {
  const method = req.method ?? "GET";
  // Strip any query string; probes hit bare paths.
  const path = (req.url ?? "/").split("?", 1)[0];

  if (method === "GET" && path === "/healthz") {
    // Liveness: if we can answer, the event loop turns. No dependency checks here
    // — a liveness probe that fails on a downstream outage would kill healthy pods.
    sendJson(res, 200, { status: "ok" });
    return;
  }

  if (method === "GET" && path === "/readyz") {
    const r = readiness(rt);
    sendJson(res, r.ready ? 200 : 503, {
      status: r.ready ? "ready" : "not-ready",
      capabilities: r.capabilities,
    });
    return;
  }

  if (method === "GET" && path === "/version") {
    sendJson(res, 200, { name: "openrotor", version: VERSION });
    return;
  }

  if (method === "POST" && path === "/run") {
    readBody(req)
      .then((raw) => {
        let body: unknown;
        try {
          body = raw.length ? JSON.parse(raw) : {};
        } catch {
          sendJson(res, 400, { error: "invalid-json", detail: "POST /run body must be JSON" });
          return;
        }
        const { rotor, inputs, session } = (body ?? {}) as { rotor?: unknown; inputs?: unknown; session?: string };
        if (rotor === undefined) {
          sendJson(res, 400, { error: "missing-rotor", detail: "POST /run requires a `rotor`" });
          return;
        }
        // Fungibility invariant (SPEC.md §17.1): this pod holds no run-critical state,
        // so it can serve any run against the (basic in-process, premium external)
        // stator. Parse → validate → execute → return the run summary.
        let doc: RotorDocument;
        try {
          doc = coerceRotor(rotor);
        } catch (err) {
          sendJson(res, 400, { error: "invalid-rotor", detail: (err as Error).message });
          return;
        }
        const { valid, errors } = validateRotor(doc);
        if (!valid) {
          sendJson(res, 422, { error: "invalid-rotor", detail: "rotor failed validation", errors });
          return;
        }
        // Reconcile the rotor's capability needs against the manifest (§3.8). An
        // unmet seam does not block the run — the step clean-refuses — but it is
        // surfaced as a warning so the operator sees the degraded path.
        const recon = rt.reconcile(doc);
        if (!recon.satisfied) {
          log.warn("rotor requires unmet capabilities", {
            rotor: `${doc.metadata.name}@${doc.metadata.version}`,
            unmet: recon.unmet.join(","),
          });
        }
        const runInputs = (inputs && typeof inputs === "object" ? inputs : {}) as Record<string, unknown>;
        // Streaming lane: when the client asks for `text/event-stream`, run the rotor
        // and stream `open → step* → terminal → done` over SSE instead of buffering a
        // single JSON reply. The run persists its tape as it goes, so a dropped client
        // reconnects via GET /runs/:id/events?from=<cursor>. This is the transport the
        // client SDK consumes; the buffered path below stays for simple callers.
        if ((req.headers.accept ?? "").includes("text/event-stream")) {
          void streamRunLive(res, doc, runInputs, { store, drain, workspace: workspaceRoot(), session });
          return;
        }
        // Pooling/affinity (§17.2–§17.4) is OPERATIONAL telemetry only (§17.6): the
        // shared pool picks a warm instance for this request, but it NEVER affects
        // what the run computes — the executor below does not consult it.
        if (doc.spec.pool) rt.plugins.pool.provision(doc.spec.pool);
        const instance = rt.plugins.pool.route(affinityHint(doc, runInputs));
        log.info("routed", { instance, state: rt.plugins.pool.instanceState(instance) });
        // Fungibility invariant (§17.1): the run-critical state lives in the shared
        // stator, not this process. A fresh plugin bundle per request keeps
        // per-run plugin state (e.g. the grounding space guard) isolated, while
        // history/cache/facts persist across requests via the shared store — so a
        // run recorded by one request replays from another.
        execute(doc, runInputs, buildBasicPlugins({ store, drain, tools: { root: workspaceRoot(), mode: toolModeFromLabels(doc.metadata.labels) } }))
          .then((result) => respondRun(res, store, doc, runInputs, result))
          .catch((err: unknown) => {
            log.error("run error", { detail: (err as Error).message });
            sendJson(res, 500, { error: "run-error", detail: (err as Error).message });
          });
      })
      .catch(() => {
        sendJson(res, 400, { error: "read-error", detail: "could not read request body" });
      });
    return;
  }

  // POST /runs/:id/resume — continue an interrupted run (§7.14).
  if (method === "POST" && path.startsWith("/runs/") && path.endsWith("/resume")) {
    const runId = decodeURIComponent(path.slice("/runs/".length, -"/resume".length));
    readBody(req)
      .then(async (raw) => {
        let body: { payload?: Record<string, unknown>; decision?: string; timeout?: boolean };
        try {
          body = raw.length ? JSON.parse(raw) : {};
        } catch {
          sendJson(res, 400, { error: "invalid-json", detail: "resume body must be JSON" });
          return;
        }
        const saved = (await store.kvGet(`run:${runId}`)) as
          | { doc: RotorDocument; inputs: Record<string, unknown>; interrupt: { stepId: string } }
          | undefined;
        if (!saved) {
          sendJson(res, 404, { error: "no-such-run", detail: `no interrupted run ${runId}` });
          return;
        }
        const payload = { ...(body.payload ?? {}), ...(body.decision ? { decision: body.decision } : {}) };
        execute(saved.doc, saved.inputs, buildBasicPlugins({ store, drain, tools: { root: workspaceRoot(), mode: toolModeFromLabels(saved.doc.metadata.labels) } }), {
          runId,
          resume: { stepId: saved.interrupt.stepId, payload, timeout: body.timeout },
        })
          .then((result) => respondRun(res, store, saved.doc, saved.inputs, result))
          .catch((err: unknown) => sendJson(res, 500, { error: "run-error", detail: (err as Error).message }));
      })
      .catch(() => sendJson(res, 400, { error: "read-error", detail: "could not read request body" }));
    return;
  }

  // GET /runs/:id/events — durable reconnect. Replay a run's SSE stream from the
  // persisted tape, resuming after the client's cursor (`Last-Event-ID` header, or
  // `?from=<seq>`). Default -1 replays from the beginning (the `open` frame at seq 0).
  if (method === "GET" && path.startsWith("/runs/") && path.endsWith("/events")) {
    const runId = decodeURIComponent(path.slice("/runs/".length, -"/events".length));
    const query = (req.url ?? "").split("?", 2)[1] ?? "";
    const fromParam = new URLSearchParams(query).get("from");
    const lastEventId = req.headers["last-event-id"];
    const cursor = Number(lastEventId ?? fromParam ?? -1);
    replayRun(res, runId, Number.isFinite(cursor) ? cursor : -1, store)
      .then((found) => {
        if (!found) sendJson(res, 404, { error: "no-such-run", detail: `no run ${runId}` });
      })
      .catch((err: unknown) => sendJson(res, 500, { error: "replay-error", detail: (err as Error).message }));
    return;
  }

  sendJson(res, 404, { error: "not-found", detail: `no route for ${method} ${path}` });
}

/** Derive a pool routing hint from `spec.affinity` (§17.4) — telemetry only. */
function affinityHint(doc: RotorDocument, inputs: Record<string, unknown>): import("./plugins/interfaces.js").AffinityHint | undefined {
  const cfg = doc.spec.affinity;
  if (!cfg?.keys?.length) return undefined;
  const parts = cfg.keys
    .map((k) => {
      if (k === "tenant") return inputs.tenant;
      if (k === "conversation") return inputs.conversation ?? inputs.conversation_id;
      if (k === "entity") return inputs.entity;
      return undefined;
    })
    .filter((v) => v !== undefined && v !== null)
    .map(String);
  return { key: parts.join(":") || doc.metadata.name, mode: cfg.mode };
}

/** Send a run summary; persist doc + inputs when the run paused so it can resume. */
async function respondRun(
  res: http.ServerResponse,
  store: Stator,
  doc: RotorDocument,
  inputs: Record<string, unknown>,
  result: import("./exec/executor.js").RunResult,
): Promise<void> {
  if (result.status === "interrupted" && result.interrupt) {
    await store.kvSet(`run:${result.run_id}`, { doc, inputs, interrupt: result.interrupt });
  }
  log.info("run complete", {
    run_id: result.run_id,
    trace_id: result.trace_id,
    rotor: `${doc.metadata.name}@${doc.metadata.version}`,
    status: result.status,
    terminal: result.terminal,
    steps: result.history.length,
    ...(result.error ? { error: result.error.name } : {}),
  });
  // Surface the taxonomy detail on a failed run so a caller (or an AI dev-ops agent)
  // gets code + remediation in the response, not just a status string.
  const error = result.error
    ? (() => {
        const d = describe(result.error.name);
        return { ...result.error, category: d.category, retryable: d.retryable, severity: d.severity, remediation: d.remediation };
      })()
    : undefined;
  sendJson(res, 200, {
    run_id: result.run_id,
    trace_id: result.trace_id,
    status: result.status,
    terminal: result.terminal,
    outputs: result.outputs,
    history: result.history,
    interrupt: result.interrupt,
    budget: result.budget,
    error,
  });
}

/** Buffer a request body with a hard cap — a probe shell should not be a sink. */
function readBody(req: http.IncomingMessage, limit = 1_000_000): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    req.on("data", (chunk: Buffer) => {
      size += chunk.length;
      if (size > limit) {
        reject(new Error("body too large"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

/**
 * Start the probe/run HTTP server on `port` (default 8080, the EXPOSE'd port).
 * One `Runtime` is constructed per instance — it owns the capability registry the
 * readiness probe reads. Returns the `http.Server` so callers can close it.
 */
export function startServer(
  port: number = DEFAULT_PORT,
  rt: Runtime = new Runtime(),
  store: Stator = statorFromEnv(),
  drain: DrainPlugin = drainFromEnv(),
): http.Server {
  const server = http.createServer((req, res) => handle(rt, store, drain, req, res));
  server.listen(port, () => {
    log.info("runtime listening", { port, version: VERSION });
  });
  return server;
}

/**
 * Graceful shutdown: flush the drain (within the pod's termination grace window),
 * release the stator, then close the server. Ordered so buffered telemetry is
 * delivered before the process exits.
 */
export async function shutdown(server: http.Server, store: Stator, drain: DrainPlugin): Promise<void> {
  try {
    await drain.close();
  } catch (err) {
    log.error("drain flush on shutdown failed", { detail: (err as Error).message });
  }
  // Async backends (pgvector) expose `shutdown()` to flush write-through before
  // releasing the client; sync backends only have `close()`.
  const s = store as Stator & { shutdown?: () => Promise<void> };
  if (s.shutdown) {
    try {
      await s.shutdown();
    } catch (err) {
      log.error("stator flush on shutdown failed", { detail: (err as Error).message });
    }
  } else {
    store.close?.();
  }
  await new Promise<void>((resolve) => server.close(() => resolve()));
}

// Run when invoked directly (as `node dist/server.js`), not when imported.
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  void (async () => {
    const port = Number(process.env.PORT ?? process.env.ROTOR_PORT ?? DEFAULT_PORT);
    // Async builder so a `pgvector` backend connects + hydrates before serving.
    const store = await statorFromEnvAsync();
    const drain = drainFromEnv();
    const server = startServer(Number.isFinite(port) ? port : DEFAULT_PORT, new Runtime(), store, drain);
    for (const sig of ["SIGTERM", "SIGINT"] as const) {
      process.on(sig, () => {
        log.info("shutting down", { signal: sig });
        void shutdown(server, store, drain).then(() => process.exit(0));
      });
    }
  })();
}
