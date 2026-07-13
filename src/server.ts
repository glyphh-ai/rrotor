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
import { statorFromEnv } from "./exec/stator.js";
import { drainFromEnv } from "./plugins/drain.js";
import { log } from "./obs/logger.js";
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
        const { rotor, inputs } = (body ?? {}) as { rotor?: unknown; inputs?: unknown };
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
        // Fungibility invariant (§17.1): the run-critical state lives in the shared
        // stator, not this process. A fresh plugin bundle per request keeps
        // per-run plugin state (e.g. the grounding space guard) isolated, while
        // history/cache/facts persist across requests via the shared store — so a
        // run recorded by one request replays from another.
        execute(doc, runInputs, buildBasicPlugins({ store, drain }))
          .then((result) => {
            log.info("run complete", {
              run_id: result.run_id,
              rotor: `${doc.metadata.name}@${doc.metadata.version}`,
              status: result.status,
              terminal: result.terminal,
              steps: result.history.length,
            });
            sendJson(res, 200, {
              run_id: result.run_id,
              status: result.status,
              terminal: result.terminal,
              outputs: result.outputs,
              history: result.history,
            });
          })
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

  sendJson(res, 404, { error: "not-found", detail: `no route for ${method} ${path}` });
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
  store.close?.();
  await new Promise<void>((resolve) => server.close(() => resolve()));
}

// Run when invoked directly (as `node dist/server.js`), not when imported.
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const port = Number(process.env.PORT ?? process.env.ROTOR_PORT ?? DEFAULT_PORT);
  const store = statorFromEnv();
  const drain = drainFromEnv();
  const server = startServer(Number.isFinite(port) ? port : DEFAULT_PORT, new Runtime(), store, drain);
  for (const sig of ["SIGTERM", "SIGINT"] as const) {
    process.on(sig, () => {
      log.info("shutting down", { signal: sig });
      void shutdown(server, store, drain).then(() => process.exit(0));
    });
  }
}
