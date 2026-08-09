/**
 * harness/server.ts — the HTTP/WS face of a HOSTED HARNESS SESSION POD.
 *
 * The cloud execution pod for INTERACTIVE sessions (chat/work/code): a
 * headless port of the desktop's Claude Agent SDK harness, served through
 * rrotor's existing conventions (node:http only, introspection auth,
 * dependency-free WS codec). This is a DISTINCT mode/entry — it shares
 * nothing stateful with the rotor loop server (src/server.ts) and never
 * disturbs it; the same pod image runs either (src/pod.ts dispatches).
 *
 *   GET  /healthz              liveness (open)
 *   GET  /version              build identity + frame wire version (open)
 *   POST /run                  start a harness session run → { runId }
 *                              (body `threadId` scopes the recorded transcript
 *                              to the CLIENT's thread id — `c<ts36>` — while
 *                              auth keeps binding `sessionId` to the token;
 *                              absent → the transcript keys by sessionId.
 *                              body `mcpServers` lends the run HTTP MCP
 *                              servers — loopback-or-https, cap 4 — the
 *                              desktop's loopback tool surface in LOCAL mode)
 *   GET  /runs/:id             run status (+ pending ask/approval ids)
 *   GET  /runs/:id/frames      replay the frame tape (?from=<seq>, exclusive)
 *   POST /runs/:id/answer      resolve a paused ask/approval frame
 *   POST /runs/:id/stop        abort the run (emits done{stopped:true})
 *   GET  /threads              thread list, newest first (metadata only)
 *   GET  /threads/:id          full transcript (latest 500 messages)
 *   PUT  /threads/:id          client LWW push (applies only when newer)
 *   DELETE /threads/:id        tombstone (hidden from list; reads 404)
 *   GET  /ws (upgrade)         the live frame stream: send
 *                              {type:"attach",run_id,from?} → replay + live
 *                              (bearer via Authorization header, or `?token=`
 *                              for browser clients that cannot set WS headers)
 *
 * Threads persist to the STATOR (harness/threads.ts) when the pgvector stator
 * is configured (ROTOR_STATOR_BACKEND=pgvector + ROTOR_STATOR_URL — the same
 * scoped DSN the memory side uses); without it the /threads routes answer 503
 * and runs are unrecorded. Every thread is OWNER-SCOPED to the introspected
 * token's org/user (the Principal on the auth decision): runs record under it
 * and the /threads routes filter by it, so a pod shared by an org never leaks
 * threads across users. No principal (auth off, or the control plane sent
 * none) → thread routes 503 and runs are unrecorded.
 *
 * Auth: the same OPTIONAL introspection gate as the rotor data plane
 * (ROTOR_AUTH_INTROSPECT_URL + service token + session binding; fail closed
 * when configured; probes stay open). CORS is wildcard on the whole surface
 * (bearer-authed, no cookies): OPTIONS preflights 204 pre-gate, and every
 * response — errors included — carries Access-Control-Allow-Origin.
 *
 * LOCAL MODE (desktop/CLI turns): the pod runs auth-OFF on the user's machine
 * (it cannot hold the service token) and therefore BINDS LOOPBACK ONLY
 * (resolveBind; ROTOR_BIND overrides, loudly). Runs there pass the USER's
 * `gy_at_` access token as the runtime token (metering lands on the user) and
 * lend the desktop's loopback MCP server via body `mcpServers`. No stator
 * locally → thread recording is off, silently — the client's LWW push owns
 * persistence.
 *
 * CONCURRENCY follows the pod's SHAPE: a shared pod (auth on, no bound
 * ROTOR_SESSION_ID) defaults to 24 concurrent runs because it serves every
 * user of its region; a dedicated or local pod defaults to 1, matching the
 * one session it exists for. HARNESS_MAX_RUNS overrides either. At 1 a shared
 * pod 409s every concurrent turn — and a PARKED approval holds the only slot
 * for its full timeout, which reads to users as "the runtime is down".
 * Finished runs stay replayable until evicted (last 8) — the in-memory
 * stand-in for stator-backed history.
 */

import * as http from "node:http";
import { fileURLToPath } from "node:url";
import type { Duplex } from "node:stream";

import { VERSION } from "../version.js";
import { log } from "../obs/logger.js";
import type { Logger } from "../obs/logger.js";
import { introspectorFromEnv, disabledIntrospector, bearerFromHeader } from "../auth/introspect.js";
import { statorRecall, statorWrite, type RecallRequest, type WriteRequest } from "./stator-api.js";
import type { Introspector, Principal, AuthDecision } from "../auth/introspect.js";
import { acceptKey, encodeFrame, FrameDecoder } from "../transport/ws.js";
import { HARNESS_WIRE_VERSION } from "./frames.js";
import type { WireFrame } from "./frames.js";
import { resolveRunConfig, BadRunRequest } from "./config.js";
import type { RunRequestBody } from "./config.js";
import { HarnessSession, mintRunId } from "./session.js";
import { runHarness } from "./engine.js";
import type { EngineDeps } from "./engine.js";
import { ThreadRecorder, threadStoreFromEnv, warnIfUnrecordable, parseThreadMsgs, parseMode } from "./threads.js";
import type { ThreadStore, ThreadPut } from "./threads.js";

const DEFAULT_PORT = 8080;
const FINISHED_KEEP = 8;
/** POST /run body ceiling — generous because a turn may carry base64 images. */
const RUN_BODY_LIMIT = 24_000_000;

/** Injectable seams for tests + the pod entry. */
export interface HarnessServerOptions {
  auth?: Introspector;
  engine?: EngineDeps;
  env?: NodeJS.ProcessEnv;
  /** Concurrent-run cap; defaults from HARNESS_MAX_RUNS (min 1). */
  maxRuns?: number;
  /** Inject a thread store (tests) — pass null to force persistence off.
   *  Omitted → built from the stator env (threadStoreFromEnv). */
  threads?: ThreadStore | null;
}

// CORS: web clients call the data plane cross-origin by design (browser at
// the app origin → POST {pod}/run). Auth is bearer-only — no cookies — so a
// wildcard origin is correct. EVERY response carries ACAO, errors included:
// a 401 without it reads as a CORS mystery in the browser, not an auth
// failure. Preflights answer 204 before the auth gate (they carry no
// Authorization by design).
const CORS_ORIGIN = { "access-control-allow-origin": "*" };
const CORS_PREFLIGHT = {
  ...CORS_ORIGIN,
  "access-control-allow-methods": "GET, POST, PUT, DELETE, OPTIONS",
  "access-control-allow-headers": "authorization, content-type",
  "access-control-max-age": "86400",
};

function sendJson(res: http.ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(payload),
    "cache-control": "no-store",
    ...CORS_ORIGIN,
  });
  res.end(payload);
}

function readBody(req: http.IncomingMessage, limit = 4_000_000): Promise<string> {
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

/** The pod's run registry: live + recently finished sessions, replay-capable. */
class RunRegistry {
  private readonly runs = new Map<string, HarnessSession>();

  constructor(private readonly maxLive: number) {}

  get(runId: string): HarnessSession | undefined {
    return this.runs.get(runId);
  }

  liveCount(): number {
    let n = 0;
    for (const s of this.runs.values()) if (s.status === "running") n++;
    return n;
  }

  /** Admit a new session or return null when the pod is at capacity. */
  admit(session: HarnessSession): boolean {
    if (this.liveCount() >= this.maxLive) return false;
    this.runs.set(session.runId, session);
    this.evict();
    return true;
  }

  /** Keep the most recent FINISHED runs replayable; drop the oldest beyond. */
  private evict(): void {
    const finished = [...this.runs.values()].filter((s) => s.status !== "running");
    if (finished.length <= FINISHED_KEEP) return;
    finished.sort((a, b) => a.startedAt - b.startedAt);
    for (const s of finished.slice(0, finished.length - FINISHED_KEEP)) this.runs.delete(s.runId);
  }

  stopAll(): void {
    for (const s of this.runs.values()) if (s.status === "running") s.stop();
  }
}

function handle(reg: RunRegistry, opts: HarnessServerOptions, threads: Promise<ThreadStore | null>, req: http.IncomingMessage, res: http.ServerResponse, auth: Introspector): void {
  const method = req.method ?? "GET";
  const path = (req.url ?? "/").split("?", 1)[0];

  if (method === "OPTIONS") {
    res.writeHead(204, CORS_PREFLIGHT);
    res.end();
    return;
  }
  if (method === "GET" && path === "/healthz") {
    sendJson(res, 200, { status: "ok", mode: "harness" });
    return;
  }
  if (method === "GET" && path === "/version") {
    sendJson(res, 200, { name: "rrotor", mode: "harness", version: VERSION, wire: HARNESS_WIRE_VERSION });
    return;
  }

  // Data-plane auth gate — same shape as src/server.ts: probes stay open,
  // everything below validates the bearer when an introspector is configured.
  if (auth.enabled) {
    const bearer = bearerFromHeader(req.headers.authorization);
    void auth
      .authorize(bearer)
      .then((decision) => {
        if (!decision.ok) {
          log.warn("auth denied", { path, method, reason: decision.reason ?? "denied" });
          sendJson(res, decision.status, { error: "unauthorized", detail: decision.reason });
          return;
        }
        dispatch(reg, opts, threads, { principal: decision.principal, sessionId: decision.sessionId, enabled: true }, req, res, method, path);
      })
      .catch((err: unknown) => {
        log.error("auth check error", { detail: (err as Error).message });
        sendJson(res, 401, { error: "unauthorized" });
      });
    return;
  }
  dispatch(reg, opts, threads, { enabled: false }, req, res, method, path);
}

/** What the auth gate resolved for this request: the token's owner and the
 *  session the token is bound to (both absent when auth is disabled), plus
 *  whether enforcement is on at all — auth OFF means LOCAL mode, the only
 *  mode that may honor a caller-named `workdir`. */
type AuthContext = Pick<AuthDecision, "principal" | "sessionId"> & { enabled: boolean };

/** Run a thread route against the store, owner-scoped — 503 when persistence
 *  is off or the caller resolved to no principal (threads are per-user; an
 *  unattributed caller cannot be scoped). */
function withThreads(threads: Promise<ThreadStore | null>, principal: Principal | undefined, res: http.ServerResponse, fn: (s: ThreadStore, p: Principal) => Promise<void>): void {
  void threads
    .then((s) => {
      if (!s) {
        sendJson(res, 503, { error: "no-thread-store", detail: "thread persistence needs the pgvector stator (ROTOR_STATOR_BACKEND=pgvector + ROTOR_STATOR_URL)" });
        return;
      }
      if (!principal) {
        sendJson(res, 503, { error: "no-principal", detail: "thread routes need introspection auth — the token's org/user scopes every read and write" });
        return;
      }
      return fn(s, principal);
    })
    .catch((err: unknown) => {
      log.error("thread route failed", { detail: (err as Error).message });
      sendJson(res, 500, { error: "thread-store-error" });
    });
}

function dispatch(reg: RunRegistry, opts: HarnessServerOptions, threads: Promise<ThreadStore | null>, authn: AuthContext, req: http.IncomingMessage, res: http.ServerResponse, method: string, path: string): void {
  if (method === "POST" && path === "/run") {
    // A turn may carry pasted images (base64), so /run takes a larger body
    // than the rest of the surface.
    readBody(req, RUN_BODY_LIMIT)
      .then((raw) => {
        let body: RunRequestBody;
        try {
          body = raw.length ? (JSON.parse(raw) as RunRequestBody) : {};
        } catch {
          sendJson(res, 400, { error: "invalid-json", detail: "POST /run body must be JSON" });
          return;
        }
        // The Authorization bearer IS the session's runtime token — default
        // the body field from it so callers don't send the same token twice
        // (an explicit body `runtimeToken` still overrides).
        if (typeof body.runtimeToken !== "string" || !body.runtimeToken.trim()) {
          const bearer = bearerFromHeader(req.headers.authorization);
          if (bearer) body.runtimeToken = bearer;
        }
        // PER-RUN session binding (the shared-pod half of the introspector's
        // two modes): the token names the session it is bound to; a body
        // sessionId that disagrees is the dedicated pod's "session mismatch",
        // judged here where a shared pod can. An absent body sessionId adopts
        // the token's, so the run and its workspace key by the right session.
        if (authn.sessionId) {
          const bodySession = typeof body.sessionId === "string" ? body.sessionId.trim() : "";
          if (bodySession && bodySession !== authn.sessionId) {
            log.warn("run session mismatch", { run_session: bodySession });
            sendJson(res, 401, { error: "unauthorized", detail: "session mismatch" });
            return;
          }
          body.sessionId = authn.sessionId;
        }
        const runId = mintRunId();
        let cfg;
        try {
          // A caller-named `workdir` is honored ONLY in local mode (auth off,
          // loopback-bound, the user's own machine); a cloud pod rejects it.
          cfg = resolveRunConfig(runId, body, opts.env ?? process.env, { allowWorkdir: !authn.enabled });
        } catch (err) {
          if (err instanceof BadRunRequest) {
            sendJson(res, 400, { error: "bad-run", detail: err.message });
            return;
          }
          throw err;
        }
        const session = new HarnessSession({ runId, sessionId: cfg.sessionId });
        if (!reg.admit(session)) {
          sendJson(res, 409, { error: "at-capacity", detail: "this pod is serving its maximum concurrent runs" });
          return;
        }
        // Record the run into its thread BEFORE the engine emits: the user
        // turn persists at start, frames stream into messages (threads.ts),
        // all owned by the caller's introspected principal.
        new ThreadRecorder(threads, cfg, authn.principal).attach(session);
        // Fire the run; frames stream via /ws + /runs/:id/frames. Never throws.
        void runHarness(session, cfg, opts.engine ?? {});
        log.info("run accepted", {
          run_id: runId,
          session: cfg.sessionId || undefined,
          mode: cfg.mode,
          permission: cfg.permission,
          // The cwd the run's tools act on — a local run names the user's own
          // folder, so it is logged explicitly.
          workdir: cfg.workdir,
        });
        sendJson(res, 200, { runId, sessionId: cfg.sessionId, wire: HARNESS_WIRE_VERSION });
      })
      .catch(() => sendJson(res, 400, { error: "read-error", detail: "could not read request body" }));
    return;
  }

  const runMatch = /^\/runs\/([^/]+)(\/(frames|answer|stop))?$/.exec(path);
  if (runMatch) {
    const runId = decodeURIComponent(runMatch[1]);
    const sub = runMatch[3] ?? "";
    const session = reg.get(runId);
    if (!session) {
      sendJson(res, 404, { error: "no-such-run", detail: `no run ${runId}` });
      return;
    }

    if (method === "GET" && sub === "") {
      sendJson(res, 200, { runId, sessionId: session.sessionId, status: session.status, pending: session.pendingIds() });
      return;
    }
    if (method === "GET" && sub === "frames") {
      const query = (req.url ?? "").split("?", 2)[1] ?? "";
      const from = Number(new URLSearchParams(query).get("from") ?? -1);
      sendJson(res, 200, { runId, status: session.status, frames: session.framesSince(Number.isFinite(from) ? from : -1) });
      return;
    }
    if (method === "POST" && sub === "answer") {
      readBody(req)
        .then((raw) => {
          let body: { id?: unknown; allow?: unknown; answers?: unknown };
          try {
            body = raw.length ? JSON.parse(raw) : {};
          } catch {
            sendJson(res, 400, { error: "invalid-json", detail: "answer body must be JSON" });
            return;
          }
          const id = typeof body.id === "string" ? body.id : "";
          if (!id) {
            sendJson(res, 400, { error: "bad-answer", detail: "`id` (the approval/ask frame id) is required" });
            return;
          }
          const ok = session.answer(id, {
            ...(body.allow !== undefined ? { allow: Boolean(body.allow) } : {}),
            ...(Array.isArray(body.answers) ? { answers: body.answers.map(String) } : {}),
          });
          if (!ok) {
            sendJson(res, 404, { error: "no-such-question", detail: `nothing awaits an answer under ${id}` });
            return;
          }
          sendJson(res, 200, { ok: true });
        })
        .catch(() => sendJson(res, 400, { error: "read-error", detail: "could not read request body" }));
      return;
    }
    if (method === "POST" && sub === "stop") {
      session.stop();
      sendJson(res, 200, { ok: true, status: session.status });
      return;
    }
  }

  // ── the STATOR API (recall/write the regional memory plane; owner-scoped) ────
  if (method === "POST" && (path === "/stator/recall" || path === "/stator/write")) {
    const principal = authn.principal;
    if (!principal) {
      sendJson(res, 503, { error: "no-principal", detail: "the stator API needs introspection auth — the token's org/user scopes every read and write" });
      return;
    }
    readBody(req, RUN_BODY_LIMIT)
      .then(async (raw) => {
        let body: Record<string, unknown>;
        try { body = raw.length ? (JSON.parse(raw) as Record<string, unknown>) : {}; }
        catch { sendJson(res, 400, { error: "invalid-json", detail: "body must be JSON" }); return; }
        try {
          const result = path === "/stator/recall"
            ? await statorRecall(principal, body as RecallRequest)
            : await statorWrite(principal, body as WriteRequest);
          sendJson(res, 200, result);
        } catch (err) {
          log.error("stator api failed", { path, detail: (err as Error).message });
          sendJson(res, 500, { error: "stator-error", detail: (err as Error).message });
        }
      })
      .catch(() => sendJson(res, 500, { error: "stator-error" }));
    return;
  }

  if (method === "GET" && path === "/threads") {
    withThreads(threads, authn.principal, res, async (s, p) => sendJson(res, 200, { threads: await s.list(p) }));
    return;
  }
  const threadMatch = /^\/threads\/([^/]+)$/.exec(path);
  if (threadMatch) {
    const threadId = decodeURIComponent(threadMatch[1]);
    if (method === "GET") {
      withThreads(threads, authn.principal, res, async (s, p) => {
        const thread = await s.get(p, threadId);
        if (!thread) sendJson(res, 404, { error: "no-such-thread", detail: `no thread ${threadId}` });
        else sendJson(res, 200, thread);
      });
      return;
    }
    if (method === "PUT") {
      readBody(req)
        .then((raw) => {
          let body: { title?: unknown; mode?: unknown; source?: unknown; messages?: unknown; updatedAt?: unknown };
          try {
            body = raw.length ? JSON.parse(raw) : {};
          } catch {
            sendJson(res, 400, { error: "invalid-json", detail: "thread body must be JSON" });
            return;
          }
          if (typeof body.updatedAt !== "number" || !Number.isFinite(body.updatedAt)) {
            sendJson(res, 400, { error: "bad-thread", detail: "`updatedAt` (epoch ms) is required — it is the LWW stamp" });
            return;
          }
          const mode = parseMode(body.mode);
          const messages = parseThreadMsgs(body.messages);
          const patch: ThreadPut = {
            ...(typeof body.title === "string" ? { title: body.title } : {}),
            ...(mode ? { mode } : {}),
            ...(body.source !== undefined ? { source: body.source } : {}),
            ...(messages ? { messages } : {}),
            updatedAt: body.updatedAt,
          };
          withThreads(threads, authn.principal, res, async (s, p) => {
            const r = await s.put(p, threadId, patch);
            if (r.applied) sendJson(res, 200, { ok: true, updatedAt: r.updatedAt });
            // Another principal's id reads as absent — existence is not revealed.
            else if (!r.owned) sendJson(res, 404, { error: "no-such-thread", detail: `no thread ${threadId}` });
            else sendJson(res, 409, { error: "stale", detail: "a newer write holds this thread", updatedAt: r.updatedAt });
          });
        })
        .catch(() => sendJson(res, 400, { error: "read-error", detail: "could not read request body" }));
      return;
    }
    if (method === "DELETE") {
      withThreads(threads, authn.principal, res, async (s, p) => {
        const ok = await s.tombstone(p, threadId, Date.now());
        if (ok) sendJson(res, 200, { ok: true });
        else sendJson(res, 404, { error: "no-such-thread", detail: `no thread ${threadId}` });
      });
      return;
    }
  }

  sendJson(res, 404, { error: "not-found", detail: `no route for ${method} ${path}` });
}

// ── the WS frame stream ─────────────────────────────────────────────────────

const OP_TEXT = 0x1;
const OP_CLOSE = 0x8;
const OP_PING = 0x9;
const OP_PONG = 0xa;

/** Attach the harness WS endpoint: {type:"attach",run_id,from?} replays the
 *  tape past the cursor then streams live frames; {type:"ping"} round-trips.
 *  Same handshake/auth pattern as transport/ws.ts. */
function attachHarnessWs(server: http.Server, reg: RunRegistry, auth: Introspector, path = "/ws"): void {
  server.on("upgrade", (req: http.IncomingMessage, socket: Duplex) => {
    if ((req.url ?? "").split("?", 1)[0] !== path) {
      socket.destroy();
      return;
    }
    const key = req.headers["sec-websocket-key"];
    if (typeof key !== "string") {
      socket.destroy();
      return;
    }
    const complete = (): void => {
      socket.write(
        "HTTP/1.1 101 Switching Protocols\r\n" +
          "Upgrade: websocket\r\n" +
          "Connection: Upgrade\r\n" +
          `Sec-WebSocket-Accept: ${acceptKey(key)}\r\n\r\n`,
      );
      serveSocket(socket, reg);
    };
    if (auth.enabled) {
      const bearer = bearerFromUpgrade(req);
      void auth
        .authorize(bearer)
        .then((decision) => {
          if (!decision.ok) {
            rejectUpgrade(socket, decision.status, decision.reason);
            return;
          }
          complete();
        })
        .catch(() => rejectUpgrade(socket, 401));
      return;
    }
    complete();
  });
}

/** The upgrade's bearer: the Authorization header wins; browsers cannot set
 *  WS headers, so `?token=` is accepted as a fallback (the control plane's
 *  panel WS pattern — server attach/ws.ts `bearerFrom`). The same
 *  introspection gate judges it either way, and the token is never logged. */
function bearerFromUpgrade(req: http.IncomingMessage): string | undefined {
  const header = bearerFromHeader(req.headers.authorization);
  if (header) return header;
  const query = (req.url ?? "").split("?", 2)[1] ?? "";
  const token = new URLSearchParams(query).get("token")?.trim();
  return token || undefined;
}

function rejectUpgrade(socket: Duplex, status: number, reason?: string): void {
  const text = status === 403 ? "Forbidden" : "Unauthorized";
  const body = JSON.stringify({ error: "unauthorized", detail: reason });
  socket.write(
    `HTTP/1.1 ${status} ${text}\r\n` +
      "content-type: application/json; charset=utf-8\r\n" +
      `content-length: ${Buffer.byteLength(body)}\r\n` +
      "connection: close\r\n\r\n" +
      body,
  );
  socket.destroy();
}

function serveSocket(socket: Duplex, reg: RunRegistry): void {
  const decoder = new FrameDecoder();
  const send = (msg: unknown): void => {
    if (!socket.destroyed) socket.write(encodeFrame(Buffer.from(JSON.stringify(msg))));
  };
  let unsubscribe: (() => void) | null = null;
  const detach = (): void => {
    unsubscribe?.();
    unsubscribe = null;
  };

  send({ type: "ready", wire: HARNESS_WIRE_VERSION });

  socket.on("data", (chunk: Buffer) => {
    let frames;
    try {
      frames = decoder.push(chunk);
    } catch (err) {
      log.warn("harness ws decode failed", { detail: (err as Error).message });
      detach();
      socket.end(encodeFrame(Buffer.alloc(0), OP_CLOSE));
      return;
    }
    for (const f of frames) {
      if (f.opcode === OP_CLOSE) {
        detach();
        socket.end(encodeFrame(Buffer.alloc(0), OP_CLOSE));
        return;
      }
      if (f.opcode === OP_PING) {
        socket.write(encodeFrame(f.payload, OP_PONG));
        continue;
      }
      if (f.opcode === OP_PONG) continue;
      if (f.opcode !== OP_TEXT) {
        send({ type: "error", detail: "binary frames are not supported" });
        continue;
      }
      let msg: { type?: string; run_id?: string; from?: number };
      try {
        msg = JSON.parse(f.payload.toString("utf8")) as typeof msg;
      } catch {
        send({ type: "error", detail: "control message must be JSON" });
        continue;
      }
      if (msg.type === "ping") {
        send({ type: "pong" });
        continue;
      }
      if (msg.type === "attach") {
        const session = reg.get(String(msg.run_id ?? ""));
        if (!session) {
          send({ type: "error", detail: `no run ${String(msg.run_id ?? "")}` });
          continue;
        }
        detach(); // one attachment per socket; a re-attach replaces the old one
        unsubscribe = session.subscribe((frame: WireFrame) => send(frame), Number.isFinite(msg.from) ? Number(msg.from) : -1);
        continue;
      }
      send({ type: "error", detail: "unknown control type" });
    }
  });

  socket.on("close", detach);
  socket.on("error", () => {
    detach();
    socket.destroy();
  });
}

// ── lifecycle ────────────────────────────────────────────────────────────────

const LOOPBACK_BINDS = ["127.0.0.1", "localhost", "::1"];

/** The pod's bind address. Auth ON → wide (0.0.0.0): the introspection gate is
 *  the boundary. Auth OFF → LOOPBACK ONLY: this is the LOCAL desktop/CLI pod
 *  (a user machine cannot hold the service token) and the loopback bind IS
 *  its security boundary. ROTOR_BIND overrides explicitly — binding wide with
 *  auth off warns loudly, because then anything that reaches the host drives
 *  the pod. Exported for tests (inject a logger to capture the warn). */
export function resolveBind(env: NodeJS.ProcessEnv, authEnabled: boolean, logger: Logger = log): string {
  const bind = env.ROTOR_BIND ?? (authEnabled ? "0.0.0.0" : "127.0.0.1");
  if (!authEnabled && !LOOPBACK_BINDS.includes(bind)) {
    logger.warn("harness bound WIDE with auth OFF — anything that reaches this host can drive the pod (set ROTOR_BIND=127.0.0.1 or enable introspection)", { bind });
  }
  return bind;
}

/** Start the harness pod server. Returns the http.Server (tests close it). */
export function startHarnessServer(port: number = DEFAULT_PORT, opts: HarnessServerOptions = {}): http.Server {
  const env = opts.env ?? process.env;
  const auth = opts.auth ?? introspectorFromEnv(env);
  // CONCURRENCY: a DEDICATED pod serves one provisioned session, so 1 was the
  // right v1 default — but a SHARED pod (auth on, many users) 409s every
  // concurrent turn at 1, and a PARKED approval holds the slot for its whole
  // timeout, locking everyone else out. So the default follows the pod's
  // shape: shared → 24, dedicated/local → 1. HARNESS_MAX_RUNS overrides both.
  const sharedPod = auth.enabled && !env.ROTOR_SESSION_ID;
  const defaultRuns = sharedPod ? 24 : 1;
  const maxRuns = Math.max(1, opts.maxRuns ?? (Number(env.HARNESS_MAX_RUNS ?? defaultRuns) || defaultRuns));
  const reg = new RunRegistry(maxRuns);
  const threads = opts.threads !== undefined ? Promise.resolve(opts.threads) : threadStoreFromEnv(env);
  warnIfUnrecordable(env, auth.enabled);
  const bind = resolveBind(env, auth.enabled);
  const server = http.createServer((req, res) => handle(reg, opts, threads, req, res, auth));
  attachHarnessWs(server, reg, auth);
  (server as http.Server & { __registry?: RunRegistry }).__registry = reg;
  server.on("close", () => {
    // Release the stator connection with the pod (injected stores stay the
    // injector's to close — ThreadStore only ends connections it opened).
    void threads.then((s) => s?.close()).catch(() => {});
  });
  server.listen(port, bind, () => {
    log.info("harness pod listening", { port, bind, version: VERSION, wire: HARNESS_WIRE_VERSION, max_runs: maxRuns, shared: sharedPod });
  });
  return server;
}

/** Boot the harness pod for real: start + graceful SIGTERM/SIGINT shutdown
 *  (stop live runs so they emit done{stopped}, then close). Never resolves. */
export async function serveHarness(port: number = DEFAULT_PORT, opts: HarnessServerOptions = {}): Promise<never> {
  const auth = opts.auth ?? introspectorFromEnv(opts.env ?? process.env);
  if (auth.enabled) log.info("harness data-plane auth enabled (introspection)", {});
  const server = startHarnessServer(port, { ...opts, auth });
  for (const sig of ["SIGTERM", "SIGINT"] as const) {
    process.on(sig, () => {
      log.info("harness shutting down", { signal: sig });
      (server as http.Server & { __registry?: RunRegistry }).__registry?.stopAll();
      server.close(() => process.exit(0));
    });
  }
  return new Promise<never>(() => {});
}

export { disabledIntrospector };

// Run when invoked directly (`node dist/harness/server.js`), not when imported.
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const port = Number(process.env.PORT ?? process.env.ROTOR_PORT ?? DEFAULT_PORT);
  void serveHarness(port);
}
