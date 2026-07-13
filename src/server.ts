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
 *   GET  /readyz    readiness — the runtime's capability manifest is servable
 *   GET  /version   build identity (the pinned runtime version)
 *   POST /run       accept a rotor + inputs; 501 until the executor is wired
 *
 * `startServer(port)` returns the listening `http.Server`. The executor wiring
 * (POST /run → engine) lands in the Integrate stage; this is the k8s-probe shell.
 */

import * as http from "node:http";
import { fileURLToPath } from "node:url";

import { Runtime } from "./runtime/runtime.js";
import { VERSION } from "./version.js";
import { parseRotor, validateRotor } from "./parser/index.js";
import { execute } from "./exec/executor.js";
import { buildBasicPlugins } from "./plugins/index.js";
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
 * the pod is READY once it can advertise a manifest at all — i.e. the capability
 * registry is wired. Even a BASIC-tier runtime whose seams are still `planned`
 * (memory/models/etc.) is a legitimate, servable pod; it declines runs it cannot
 * satisfy via clean-refuse rather than by failing readiness. The probe reports the
 * per-capability ready/tier so an operator can see WHY a pod would refuse.
 */
function readiness(rt: Runtime): { ready: boolean; capabilities: Record<string, { ready: boolean; tier: string }> } {
  const manifest = rt.status();
  const capabilities: Record<string, { ready: boolean; tier: string }> = {};
  for (const [name, st] of Object.entries(manifest)) {
    capabilities[name] = { ready: st.ready, tier: st.tier };
  }
  // A manifest that exists at all means the registry negotiated — the pod can
  // accept traffic and reconcile each rotor's `requires` at load time. Until any
  // seam is live we still report ready:true so the pod joins the Service and
  // serves /version + clean-refusals; flip the gate by making this depend on a
  // required seam (e.g. `capabilities.gateway.ready`) once the executor lands.
  const ready = Object.keys(capabilities).length > 0;
  return { ready, capabilities };
}

/** The request router. Kept flat and allocation-light — this is the probe path. */
function handle(rt: Runtime, req: http.IncomingMessage, res: http.ServerResponse): void {
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
        const runInputs = (inputs && typeof inputs === "object" ? inputs : {}) as Record<string, unknown>;
        execute(doc, runInputs, buildBasicPlugins())
          .then((result) => {
            sendJson(res, 200, {
              run_id: result.run_id,
              status: result.status,
              terminal: result.terminal,
              outputs: result.outputs,
              history: result.history,
            });
          })
          .catch((err: unknown) => {
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
export function startServer(port: number = DEFAULT_PORT): http.Server {
  const rt = new Runtime();
  const server = http.createServer((req, res) => handle(rt, req, res));
  server.listen(port, () => {
    console.log(`openrotor runtime listening on :${port} (v${VERSION})`);
  });
  return server;
}

// Run when invoked directly (as `node dist/server.js`), not when imported.
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const port = Number(process.env.PORT ?? process.env.ROTOR_PORT ?? DEFAULT_PORT);
  startServer(Number.isFinite(port) ? port : DEFAULT_PORT);
}
