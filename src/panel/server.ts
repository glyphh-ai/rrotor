/**
 * panel/server.ts — the HTTP/WS face of the BROWSER-PANEL POD service.
 *
 * A pod service that streams REAL browser pixels: a headless Chromium runs in
 * the pod, each panel is an isolated browser context, and the client receives a
 * screencast over WS + sends input back (architecture-engines-memory.md §7 —
 * "surfaces render in the runtime pod and stream to the client"). It COEXISTS
 * with the harness + rotor modes and shares nothing stateful with them; the pod
 * dispatches on ROTOR_MODE=panel (pod.ts).
 *
 * Built on the pod's existing conventions: node:http only, the dependency-free
 * WS codec (transport/ws.ts), and the SAME optional introspection auth as the
 * harness/rotor data planes (fail-closed when configured, open otherwise).
 *
 *   GET  /healthz                 liveness (open)
 *   GET  /version                 build identity + panel wire version (open)
 *   GET  /panel/demo              the standalone interactive demo client (open)
 *   POST /panel/browser           { url, sessionId?, viewport?, quality? }
 *                                 → { panelId, wsPath } (launch+navigate+cast)
 *   POST /panel/browser/:id/nav   { url } — navigate an existing panel
 *   DELETE /panel/browser/:id     close the panel (context teardown)
 *   WS   /panel/browser/:id       server→client: frame/nav/closed/error;
 *                                 client→server: input events {type:'mouse'…}
 */

import * as http from "node:http";
import { fileURLToPath } from "node:url";
import type { Duplex } from "node:stream";

import { VERSION } from "../version.js";
import { log } from "../obs/logger.js";
import { introspectorFromEnv, disabledIntrospector, bearerFromHeader } from "../auth/introspect.js";
import type { Introspector } from "../auth/introspect.js";
import { acceptKey, encodeFrame, FrameDecoder } from "../transport/ws.js";
import { PANEL_WIRE_VERSION } from "./frames.js";
import type { PanelMessage } from "./frames.js";
import type { InputEvent } from "./input.js";
import { PanelRegistry, PanelAtCapacity, BadPanelRequest } from "./registry.js";
import type { BrowserDriver } from "./browser.js";
import { PlaywrightBrowserPool } from "./playwright-driver.js";
import { DEMO_HTML } from "./demo.js";

const DEFAULT_PORT = 8080;

export interface PanelServerOptions {
  auth?: Introspector;
  driver?: BrowserDriver;
  env?: NodeJS.ProcessEnv;
  /** Max concurrent panels; defaults from PANEL_MAX (min 1). */
  maxPanels?: number;
}

function sendJson(res: http.ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(payload),
    "cache-control": "no-store",
  });
  res.end(payload);
}

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

function handle(reg: PanelRegistry, req: http.IncomingMessage, res: http.ServerResponse, auth: Introspector): void {
  const method = req.method ?? "GET";
  const path = (req.url ?? "/").split("?", 1)[0];

  if (method === "GET" && path === "/healthz") {
    sendJson(res, 200, { status: "ok", mode: "panel" });
    return;
  }
  if (method === "GET" && path === "/version") {
    sendJson(res, 200, { name: "rrotor", mode: "panel", version: VERSION, wire: PANEL_WIRE_VERSION });
    return;
  }
  if (method === "GET" && path === "/panel/demo") {
    res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
    res.end(DEMO_HTML);
    return;
  }

  // Data-plane auth gate — same shape as harness/server.ts: probes + the demo
  // page stay open, everything below validates the bearer when configured.
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
        dispatch(reg, req, res, method, path);
      })
      .catch((err: unknown) => {
        log.error("auth check error", { detail: (err as Error).message });
        sendJson(res, 401, { error: "unauthorized" });
      });
    return;
  }
  dispatch(reg, req, res, method, path);
}

function dispatch(reg: PanelRegistry, req: http.IncomingMessage, res: http.ServerResponse, method: string, path: string): void {
  if (method === "POST" && path === "/panel/browser") {
    readBody(req)
      .then(async (raw) => {
        let body: { url?: unknown; sessionId?: unknown; viewport?: { width?: unknown; height?: unknown; deviceScaleFactor?: unknown }; deviceScaleFactor?: unknown; quality?: unknown };
        try {
          body = raw.length ? JSON.parse(raw) : {};
        } catch {
          sendJson(res, 400, { error: "invalid-json", detail: "POST /panel/browser body must be JSON" });
          return;
        }
        try {
          const { panelId } = await reg.open({
            url: String(body.url ?? ""),
            ...(typeof body.sessionId === "string" ? { sessionId: body.sessionId } : {}),
            ...(body.viewport ? { viewport: { width: Number(body.viewport.width), height: Number(body.viewport.height) } } : {}),
            ...(Number.isFinite(Number(body.quality)) ? { quality: Number(body.quality) } : {}),
            ...((() => { const d = Number(body.deviceScaleFactor ?? body.viewport?.deviceScaleFactor); return Number.isFinite(d) && d > 0 ? { deviceScaleFactor: d } : {}; })()),
          });
          sendJson(res, 200, { panelId, wsPath: `/panel/browser/${panelId}`, wire: PANEL_WIRE_VERSION });
        } catch (err) {
          if (err instanceof BadPanelRequest) return sendJson(res, 400, { error: "bad-panel", detail: err.message });
          if (err instanceof PanelAtCapacity) return sendJson(res, 409, { error: "at-capacity", detail: err.message });
          log.error("panel open failed", { detail: (err as Error).message });
          return sendJson(res, 502, { error: "open-failed", detail: "could not open the browser panel" });
        }
      })
      .catch(() => sendJson(res, 400, { error: "read-error", detail: "could not read request body" }));
    return;
  }

  const m = /^\/panel\/browser\/([^/]+)(\/nav)?$/.exec(path);
  if (m) {
    const panelId = decodeURIComponent(m[1]);
    const isNav = m[2] === "/nav";
    const session = reg.get(panelId);
    if (!session) {
      sendJson(res, 404, { error: "no-such-panel", detail: `no panel ${panelId}` });
      return;
    }
    if (method === "POST" && isNav) {
      readBody(req)
        .then(async (raw) => {
          let body: { url?: unknown };
          try {
            body = raw.length ? JSON.parse(raw) : {};
          } catch {
            sendJson(res, 400, { error: "invalid-json", detail: "nav body must be JSON" });
            return;
          }
          const url = typeof body.url === "string" ? body.url.trim() : "";
          if (!/^https?:\/\//i.test(url)) {
            sendJson(res, 400, { error: "bad-nav", detail: "`url` must be an http(s) URL" });
            return;
          }
          try {
            await session.navigate(url);
            sendJson(res, 200, { ok: true });
          } catch (err) {
            sendJson(res, 502, { error: "nav-failed", detail: (err as Error).message });
          }
        })
        .catch(() => sendJson(res, 400, { error: "read-error", detail: "could not read request body" }));
      return;
    }
    if (method === "DELETE" && !isNav) {
      void reg.close(panelId).then((ok) => sendJson(res, ok ? 200 : 404, ok ? { ok: true } : { error: "no-such-panel" }));
      return;
    }
  }

  sendJson(res, 404, { error: "not-found", detail: `no route for ${method} ${path}` });
}

// ── the WS panel stream ──────────────────────────────────────────────────────

const OP_TEXT = 0x1;
const OP_CLOSE = 0x8;
const OP_PING = 0x9;
const OP_PONG = 0xa;

/** Attach the panel WS: the upgrade path IS the panel id (/panel/browser/:id).
 *  On connect the session's screencast + nav messages stream out; inbound text
 *  frames are input events dispatched into the page. */
function attachPanelWs(server: http.Server, reg: PanelRegistry, auth: Introspector): void {
  server.on("upgrade", (req: http.IncomingMessage, socket: Duplex) => {
    const path = (req.url ?? "").split("?", 1)[0];
    const m = /^\/panel\/browser\/([^/]+)$/.exec(path);
    if (!m) {
      socket.destroy();
      return;
    }
    const panelId = decodeURIComponent(m[1]);
    const key = req.headers["sec-websocket-key"];
    if (typeof key !== "string") {
      socket.destroy();
      return;
    }
    const complete = (): void => {
      const session = reg.get(panelId);
      if (!session) {
        rejectUpgrade(socket, 404, `no panel ${panelId}`);
        return;
      }
      socket.write(
        "HTTP/1.1 101 Switching Protocols\r\n" +
          "Upgrade: websocket\r\n" +
          "Connection: Upgrade\r\n" +
          `Sec-WebSocket-Accept: ${acceptKey(key)}\r\n\r\n`,
      );
      serveSocket(socket, session);
    };
    if (auth.enabled) {
      const bearer = bearerFromHeader(req.headers.authorization);
      void auth
        .authorize(bearer)
        .then((decision) => (decision.ok ? complete() : rejectUpgrade(socket, decision.status, decision.reason)))
        .catch(() => rejectUpgrade(socket, 401));
      return;
    }
    complete();
  });
}

function rejectUpgrade(socket: Duplex, status: number, reason?: string): void {
  const text = status === 403 ? "Forbidden" : status === 404 ? "Not Found" : "Unauthorized";
  const body = JSON.stringify({ error: text.toLowerCase().replace(" ", "-"), detail: reason });
  socket.write(
    `HTTP/1.1 ${status} ${text}\r\n` +
      "content-type: application/json; charset=utf-8\r\n" +
      `content-length: ${Buffer.byteLength(body)}\r\n` +
      "connection: close\r\n\r\n" +
      body,
  );
  socket.destroy();
}

/** Serve one panel WS connection: fan the session's messages out, dispatch
 *  inbound input events. */
function serveSocket(socket: Duplex, session: import("./session.js").PanelSession): void {
  const decoder = new FrameDecoder();
  const send = (msg: PanelMessage): void => {
    if (!socket.destroyed) socket.write(encodeFrame(Buffer.from(JSON.stringify(msg))));
  };
  const unsubscribe = session.subscribe(send);

  socket.on("data", (chunk: Buffer) => {
    let frames;
    try {
      frames = decoder.push(chunk);
    } catch (err) {
      log.warn("panel ws decode failed", { detail: (err as Error).message });
      unsubscribe();
      socket.end(encodeFrame(Buffer.alloc(0), OP_CLOSE));
      return;
    }
    for (const f of frames) {
      if (f.opcode === OP_CLOSE) {
        unsubscribe();
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
      let ev: (InputEvent & { type?: string }) | { type?: string };
      try {
        ev = JSON.parse(f.payload.toString("utf8"));
      } catch {
        send({ type: "error", detail: "input event must be JSON" });
        continue;
      }
      if ((ev as { type?: string }).type === "ping") {
        send({ type: "pong" });
        continue;
      }
      void session.dispatch(ev as InputEvent);
    }
  });

  socket.on("close", unsubscribe);
  socket.on("error", () => {
    unsubscribe();
    socket.destroy();
  });
}

// ── lifecycle ────────────────────────────────────────────────────────────────

/** Start the panel pod server. Returns the http.Server (tests close it). */
export function startPanelServer(port: number = DEFAULT_PORT, opts: PanelServerOptions = {}): http.Server {
  const env = opts.env ?? process.env;
  const maxPanels = Math.max(1, opts.maxPanels ?? (Number(env.PANEL_MAX ?? 4) || 4));
  const auth = opts.auth ?? introspectorFromEnv(env);
  const driver = opts.driver ?? new PlaywrightBrowserPool(env.PANEL_CHROMIUM_PATH);
  const reg = new PanelRegistry(driver, maxPanels);
  const server = http.createServer((req, res) => handle(reg, req, res, auth));
  attachPanelWs(server, reg, auth);
  (server as http.Server & { __registry?: PanelRegistry }).__registry = reg;
  server.listen(port, () => {
    log.info("panel pod listening", { port, version: VERSION, wire: PANEL_WIRE_VERSION, max_panels: maxPanels });
  });
  return server;
}

/** Boot the panel pod for real: start + graceful shutdown (close every panel +
 *  the browser so no Chromium is orphaned). Never resolves. */
export async function servePanel(port: number = DEFAULT_PORT, opts: PanelServerOptions = {}): Promise<never> {
  const auth = opts.auth ?? introspectorFromEnv(opts.env ?? process.env);
  if (auth.enabled) log.info("panel data-plane auth enabled (introspection)", {});
  const server = startPanelServer(port, { ...opts, auth });
  for (const sig of ["SIGTERM", "SIGINT"] as const) {
    process.on(sig, () => {
      log.info("panel shutting down", { signal: sig });
      const reg = (server as http.Server & { __registry?: PanelRegistry }).__registry;
      void (reg ? reg.closeAll() : Promise.resolve()).finally(() => server.close(() => process.exit(0)));
    });
  }
  return new Promise<never>(() => {});
}

export { disabledIntrospector };

// Run when invoked directly (`node dist/panel/server.js`), not when imported.
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const port = Number(process.env.PORT ?? process.env.ROTOR_PORT ?? DEFAULT_PORT);
  void servePanel(port);
}
