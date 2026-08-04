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
 *
 * It ALSO hosts featherweight TERMINAL panels (architecture-engines-memory.md §7 —
 * "terminal: pty BYTES over WS → xterm.js renders locally"), reusing the SAME
 * panel-scoped token/auth/registry machinery so browser + terminal panels are one
 * consistent capability:
 *
 *   POST /panel/terminal          { sessionId?, cols?, rows?, cwd?, shell? }
 *                                 → { panelId, wsPath, wire } (spawn a pty in the
 *                                   session sandbox)
 *   DELETE /panel/terminal/:id    close the terminal (kill the pty — no orphan shell)
 *   WS   /panel/terminal/:id      server→client: {type:'data'|'exit'|'error'|'pong'};
 *                                 client→server: {type:'input'|'resize'|'ping'}
 */

import * as http from "node:http";
import * as crypto from "node:crypto";
import { fileURLToPath } from "node:url";
import type { Duplex } from "node:stream";

import { VERSION } from "../version.js";
import { log } from "../obs/logger.js";
import { bearerFromHeader } from "../auth/introspect.js";
import { panelIntrospectorFromEnv, disabledPanelIntrospector } from "./auth.js";
import type { PanelIntrospector } from "./auth.js";
import { acceptKey, encodeFrame, FrameDecoder } from "../transport/ws.js";
import { PANEL_WIRE_VERSION } from "./frames.js";
import type { PanelMessage } from "./frames.js";
import type { InputEvent } from "./input.js";
import { PanelRegistry, PanelAtCapacity, BadPanelRequest } from "./registry.js";
import type { BrowserDriver } from "./browser.js";
import { PlaywrightBrowserPool } from "./playwright-driver.js";
import { DEMO_HTML } from "./demo.js";
import { TerminalRegistry, TerminalAtCapacity, BadTerminalRequest, TERMINAL_WIRE_VERSION } from "./terminal.js";
import type { TerminalSession, TerminalMessage, TerminalInput } from "./terminal.js";
import type { TerminalDriver } from "./terminal-driver.js";
import { NodePtyDriver } from "./terminal-driver.js";

const DEFAULT_PORT = 8080;

export interface PanelServerOptions {
  auth?: PanelIntrospector;
  driver?: BrowserDriver;
  /** The pty driver for terminal panels; defaults to node-pty (NodePtyDriver). */
  terminalDriver?: TerminalDriver;
  env?: NodeJS.ProcessEnv;
  /** Max concurrent panels; defaults from PANEL_MAX (min 1). */
  maxPanels?: number;
  /** Max concurrent terminals; defaults from PANEL_TERM_MAX (min 1). */
  maxTerminals?: number;
  /**
   * The SERVICE token the CONTROL PLANE presents on `POST /panel/browser` (opening a
   * panel — no panel exists yet to scope a token to). Shared region secret. When auth is
   * enabled and this is set, open is service-gated; the browser's subsequent WS + nav/
   * close carry the PANEL-scoped token instead. Defaults from ROTOR_AUTH_SERVICE_TOKEN.
   */
  serviceToken?: string;
}

/** Constant-time string equality — never leaks length/prefix via timing. */
function safeEqual(a: string, b: string): boolean {
  const ab = Buffer.from(a);
  const bb = Buffer.from(b);
  return ab.length === bb.length && crypto.timingSafeEqual(ab, bb);
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

/** The panel data plane's auth: a panel-scoped introspector + the open-call service token. */
export interface PanelAuth {
  introspector: PanelIntrospector;
  /** The service token the control plane presents on OPEN. Empty → open is not gated. */
  serviceToken: string;
}

/** Whether enforcement is on at all (mirrors the introspector's enabled flag). */
function authEnabled(auth: PanelAuth): boolean {
  return auth.introspector.enabled;
}

/** Extract the panelId a request targets, or null for the open/collection route.
 *  Covers BOTH browser (`/panel/browser/:id[/nav]`) and terminal (`/panel/terminal/:id`)
 *  panel routes — a scoped token is bound to a panelId regardless of panel kind. */
function routePanelId(path: string): string | null {
  const m = /^\/panel\/(?:browser\/([^/]+)(?:\/nav)?|terminal\/([^/]+))$/.exec(path);
  if (!m) return null;
  const id = m[1] ?? m[2];
  return id ? decodeURIComponent(id) : null;
}

/** The registries the terminal + browser routes dispatch into. */
interface PanelRegs {
  browser: PanelRegistry;
  terminal: TerminalRegistry;
}

function handle(reg: PanelRegs, req: http.IncomingMessage, res: http.ServerResponse, auth: PanelAuth): void {
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

  // Auth gate. Probes + the demo page stay open. When enforcement is on:
  //  • OPEN (POST /panel/browser) is SERVICE-token gated — the control plane opens
  //    panels; no panel exists yet to scope a token to.
  //  • Everything for a SPECIFIC panel (/panel/browser/:id[/nav]) requires a PANEL-
  //    scoped token bound to THAT panelId (a valid token for another panel is 403).
  if (authEnabled(auth)) {
    const bearer = bearerFromHeader(req.headers.authorization);
    const panelId = routePanelId(path);
    if (method === "POST" && (path === "/panel/browser" || path === "/panel/terminal")) {
      if (!auth.serviceToken || !bearer || !safeEqual(bearer, auth.serviceToken)) {
        log.warn("panel open denied — service token required", { path, method });
        sendJson(res, 401, { error: "unauthorized", detail: "service token required to open a panel" });
        return;
      }
      dispatch(reg, req, res, method, path);
      return;
    }
    if (panelId) {
      // nav/close on a specific panel accept EITHER the trusted control plane's SERVICE
      // token (the broker forwards nav/close on behalf of the owner it already authed) OR
      // a PANEL-scoped token bound to this panelId. The browser's WS uses the panel token;
      // the broker's HTTP forward uses the service token.
      if (auth.serviceToken && bearer && safeEqual(bearer, auth.serviceToken)) {
        dispatch(reg, req, res, method, path);
        return;
      }
      void auth.introspector
        .authorizePanel(bearer, panelId)
        .then((decision) => {
          if (!decision.ok) {
            log.warn("panel auth denied", { path, method, reason: decision.reason ?? "denied" });
            sendJson(res, decision.status, { error: "unauthorized", detail: decision.reason });
            return;
          }
          dispatch(reg, req, res, method, path);
        })
        .catch((err: unknown) => {
          log.error("panel auth check error", { detail: (err as Error).message });
          sendJson(res, 401, { error: "unauthorized" });
        });
      return;
    }
    // Any other authed path with enforcement on — deny (no unscoped access).
    sendJson(res, 404, { error: "not-found", detail: `no route for ${method} ${path}` });
    return;
  }
  dispatch(reg, req, res, method, path);
}

function dispatch(regs: PanelRegs, req: http.IncomingMessage, res: http.ServerResponse, method: string, path: string): void {
  const reg = regs.browser;
  // ── terminal panel routes (pty over WS) ──────────────────────────────────────
  if (method === "POST" && path === "/panel/terminal") {
    readBody(req)
      .then(async (raw) => {
        let body: { sessionId?: unknown; cols?: unknown; rows?: unknown; cwd?: unknown; shell?: unknown };
        try {
          body = raw.length ? JSON.parse(raw) : {};
        } catch {
          sendJson(res, 400, { error: "invalid-json", detail: "POST /panel/terminal body must be JSON" });
          return;
        }
        try {
          const { panelId } = await regs.terminal.open({
            ...(typeof body.sessionId === "string" ? { sessionId: body.sessionId } : {}),
            ...(Number.isFinite(Number(body.cols)) ? { cols: Number(body.cols) } : {}),
            ...(Number.isFinite(Number(body.rows)) ? { rows: Number(body.rows) } : {}),
            ...(typeof body.cwd === "string" ? { cwd: body.cwd } : {}),
            ...(typeof body.shell === "string" ? { shell: body.shell } : {}),
          });
          sendJson(res, 200, { panelId, wsPath: `/panel/terminal/${panelId}`, wire: TERMINAL_WIRE_VERSION });
        } catch (err) {
          if (err instanceof BadTerminalRequest) return sendJson(res, 400, { error: "bad-terminal", detail: err.message });
          if (err instanceof TerminalAtCapacity) return sendJson(res, 409, { error: "at-capacity", detail: err.message });
          log.error("terminal open failed", { detail: (err as Error).message });
          return sendJson(res, 502, { error: "open-failed", detail: "could not open the terminal panel" });
        }
      })
      .catch(() => sendJson(res, 400, { error: "read-error", detail: "could not read request body" }));
    return;
  }
  const tm = /^\/panel\/terminal\/([^/]+)$/.exec(path);
  if (tm) {
    const panelId = decodeURIComponent(tm[1]);
    if (method === "DELETE") {
      const ok = regs.terminal.close(panelId);
      sendJson(res, ok ? 200 : 404, ok ? { ok: true } : { error: "no-such-panel" });
      return;
    }
    sendJson(res, 404, { error: "not-found", detail: `no route for ${method} ${path}` });
    return;
  }

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

/** The WS bearer: browsers cannot set an Authorization header on a WebSocket, so the
 *  panel token rides the query string (`?token=`) or the Authorization header (server-
 *  side / demo callers). Header wins when both are present. */
function wsBearer(req: http.IncomingMessage): string | undefined {
  const header = bearerFromHeader(req.headers.authorization);
  if (header) return header;
  try {
    const q = new URL(req.url ?? "", "http://x").searchParams.get("token");
    return q ? q.trim() : undefined;
  } catch {
    return undefined;
  }
}

/** Attach the panel WS: the upgrade path IS the panel id (/panel/browser/:id).
 *  On connect the session's screencast + nav messages stream out; inbound text
 *  frames are input events dispatched into the page. The upgrade is gated by the
 *  PANEL-scoped introspector bound to this panelId. */
function attachPanelWs(server: http.Server, regs: PanelRegs, auth: PanelAuth): void {
  server.on("upgrade", (req: http.IncomingMessage, socket: Duplex) => {
    const path = (req.url ?? "").split("?", 1)[0];
    const bm = /^\/panel\/browser\/([^/]+)$/.exec(path);
    const tm = /^\/panel\/terminal\/([^/]+)$/.exec(path);
    const m = bm ?? tm;
    if (!m) {
      socket.destroy();
      return;
    }
    const kind: "browser" | "terminal" = bm ? "browser" : "terminal";
    const panelId = decodeURIComponent(m[1]);
    const key = req.headers["sec-websocket-key"];
    if (typeof key !== "string") {
      socket.destroy();
      return;
    }
    const complete = (): void => {
      const accept = (): void =>
        void socket.write(
          "HTTP/1.1 101 Switching Protocols\r\n" +
            "Upgrade: websocket\r\n" +
            "Connection: Upgrade\r\n" +
            `Sec-WebSocket-Accept: ${acceptKey(key)}\r\n\r\n`,
        );
      if (kind === "terminal") {
        const session = regs.terminal.get(panelId);
        if (!session) {
          rejectUpgrade(socket, 404, `no panel ${panelId}`);
          return;
        }
        accept();
        serveTerminalSocket(socket, session);
        return;
      }
      const session = regs.browser.get(panelId);
      if (!session) {
        rejectUpgrade(socket, 404, `no panel ${panelId}`);
        return;
      }
      accept();
      serveSocket(socket, session);
    };
    if (authEnabled(auth)) {
      const bearer = wsBearer(req);
      void auth.introspector
        .authorizePanel(bearer, panelId)
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

/** Serve one TERMINAL WS connection: fan the pty's output out, feed inbound
 *  input/resize into the pty. Same frame codec + keepalive as the browser socket. */
function serveTerminalSocket(socket: Duplex, session: TerminalSession): void {
  const decoder = new FrameDecoder();
  const send = (msg: TerminalMessage): void => {
    if (!socket.destroyed) socket.write(encodeFrame(Buffer.from(JSON.stringify(msg))));
  };
  const unsubscribe = session.subscribe(send);

  socket.on("data", (chunk: Buffer) => {
    let frames;
    try {
      frames = decoder.push(chunk);
    } catch (err) {
      log.warn("terminal ws decode failed", { detail: (err as Error).message });
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
      let ev: TerminalInput | { type?: string };
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
      session.dispatch(ev as TerminalInput);
    }
  });

  socket.on("close", unsubscribe);
  socket.on("error", () => {
    unsubscribe();
    socket.destroy();
  });
}

// ── lifecycle ────────────────────────────────────────────────────────────────

/** Build the panel auth bundle from options/env: the panel-scoped introspector +
 *  the service token that gates the open call. */
function resolvePanelAuth(opts: PanelServerOptions, env: NodeJS.ProcessEnv): PanelAuth {
  const introspector = opts.auth ?? panelIntrospectorFromEnv(env);
  const serviceToken = opts.serviceToken ?? env.ROTOR_AUTH_SERVICE_TOKEN ?? "";
  return { introspector, serviceToken };
}

/** Start the panel pod server. Returns the http.Server (tests close it). */
export function startPanelServer(port: number = DEFAULT_PORT, opts: PanelServerOptions = {}): http.Server {
  const env = opts.env ?? process.env;
  const maxPanels = Math.max(1, opts.maxPanels ?? (Number(env.PANEL_MAX ?? 4) || 4));
  const maxTerminals = Math.max(1, opts.maxTerminals ?? (Number(env.PANEL_TERM_MAX ?? 16) || 16));
  const auth = resolvePanelAuth(opts, env);
  const driver = opts.driver ?? new PlaywrightBrowserPool(env.PANEL_CHROMIUM_PATH);
  const terminalDriver = opts.terminalDriver ?? new NodePtyDriver();
  const regs: PanelRegs = {
    browser: new PanelRegistry(driver, maxPanels),
    terminal: new TerminalRegistry(terminalDriver, maxTerminals),
  };
  const server = http.createServer((req, res) => handle(regs, req, res, auth));
  attachPanelWs(server, regs, auth);
  (server as http.Server & { __registry?: PanelRegs }).__registry = regs;
  server.listen(port, () => {
    log.info("panel pod listening", { port, version: VERSION, wire: PANEL_WIRE_VERSION, terminal_wire: TERMINAL_WIRE_VERSION, max_panels: maxPanels, max_terminals: maxTerminals, auth: auth.introspector.enabled });
  });
  return server;
}

/** Boot the panel pod for real: start + graceful shutdown (close every panel +
 *  the browser so no Chromium is orphaned). Never resolves. */
export async function servePanel(port: number = DEFAULT_PORT, opts: PanelServerOptions = {}): Promise<never> {
  const env = opts.env ?? process.env;
  const auth = resolvePanelAuth(opts, env);
  if (auth.introspector.enabled) log.info("panel data-plane auth enabled (introspection)", {});
  else log.warn("panel data-plane auth DISABLED (no ROTOR_AUTH_INTROSPECT_URL) — dev only", {});
  const server = startPanelServer(port, { ...opts, auth: auth.introspector, serviceToken: auth.serviceToken });
  for (const sig of ["SIGTERM", "SIGINT"] as const) {
    process.on(sig, () => {
      log.info("panel shutting down", { signal: sig });
      const regs = (server as http.Server & { __registry?: PanelRegs }).__registry;
      // Tear down BOTH registries: no orphaned Chromium AND no orphaned shells.
      const teardown = regs ? Promise.all([regs.browser.closeAll(), regs.terminal.closeAll()]) : Promise.resolve();
      void teardown.finally(() => server.close(() => process.exit(0)));
    });
  }
  return new Promise<never>(() => {});
}

export { disabledPanelIntrospector };

// Run when invoked directly (`node dist/panel/server.js`), not when imported.
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const port = Number(process.env.PORT ?? process.env.ROTOR_PORT ?? DEFAULT_PORT);
  void servePanel(port);
}
