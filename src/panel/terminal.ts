/**
 * panel/terminal.ts — ONE terminal panel (a pty in the pod) + the pod's terminal
 * table, mirroring session.ts + registry.ts for browser panels.
 *
 * A {@link TerminalSession} owns a {@link Pty} and:
 *   • fans pty OUTPUT out to attached subscribers as `{type:"data",data}` (data =
 *     base64 of the raw pty bytes, so binary-safe over the JSON text WS).
 *   • takes client INPUT (`{type:"input",data}` keystrokes) and `{type:"resize",
 *     cols,rows}` off the WS and drives the pty.
 *   • emits `{type:"exit",code}` when the shell ends, and tears the pty down on
 *     `close()` — no orphaned shells.
 *
 * The {@link TerminalRegistry} turns a `POST /panel/terminal` into a running pty:
 * it resolves the per-session SANDBOX dir (a pty must NOT escape to arbitrary host
 * paths — cwd defaults to and is clamped under the sandbox), spawns, registers,
 * and a capacity cap refuses new terminals past the limit. `closeAll()` (pod
 * shutdown) kills every pty so nothing is orphaned.
 */

import { mkdir } from "node:fs/promises";
import { join, resolve, sep } from "node:path";

import { log } from "../obs/logger.js";
import type { Pty, TerminalDriver } from "./terminal-driver.js";

/** Bump on any breaking change to the terminal wire shapes. */
export const TERMINAL_WIRE_VERSION = "glyphh.terminal/v1";

/** A server→client terminal message (each a JSON text frame). */
export type TerminalMessage =
  | { type: "ready"; panelId: string; wire: string; cols: number; rows: number }
  | { type: "data"; data: string } // data = base64 of raw pty bytes (binary-safe)
  | { type: "exit"; code: number; signal?: number }
  | { type: "error"; detail: string }
  | { type: "pong" };

/** A client→server terminal message. */
export type TerminalInput =
  | { type: "input"; data: string } // keystrokes (utf8)
  | { type: "resize"; cols: number; rows: number }
  | { type: "ping" };

/** Clamp a terminal dimension to a sane range (xterm sends cols/rows). */
function clampCell(v: unknown, fallback: number): number {
  const n = Math.floor(Number(v));
  if (!Number.isFinite(n) || n <= 0) return fallback;
  return Math.min(1000, Math.max(1, n));
}

export interface TerminalSessionOptions {
  panelId: string;
  sessionId?: string;
  pty: Pty;
  cols: number;
  rows: number;
}

/** Mint a terminal panel id (distinct prefix from browser `pnl-`). */
export function mintTerminalId(): string {
  return `trm-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}`;
}

export class TerminalSession {
  readonly panelId: string;
  readonly sessionId: string;
  status: "live" | "closed" = "live";
  private cols: number;
  private rows: number;
  private readonly pty: Pty;
  private readonly subs = new Set<(m: TerminalMessage) => void>();
  private readonly logger;
  private exited: { code: number; signal?: number } | null = null;

  constructor(opts: TerminalSessionOptions) {
    this.panelId = opts.panelId;
    this.sessionId = opts.sessionId ?? "";
    this.pty = opts.pty;
    this.cols = opts.cols;
    this.rows = opts.rows;
    this.logger = log.child({ panel_id: this.panelId });
    // Pty output → fan out as base64 data (binary-safe over a JSON WS).
    this.pty.onData((chunk) => {
      this.emit({ type: "data", data: Buffer.from(chunk, "utf8").toString("base64") });
    });
    this.pty.onExit((ev) => {
      this.exited = { code: ev.exitCode, ...(ev.signal !== undefined ? { signal: ev.signal } : {}) };
      this.emit({ type: "exit", code: ev.exitCode, ...(ev.signal !== undefined ? { signal: ev.signal } : {}) });
      this.logger.info("pty exited", { code: ev.exitCode, signal: ev.signal });
      // A shell that ends on its own is done — reap it so nothing lingers.
      this.status = "closed";
    });
  }

  /** Fan one message to every live subscriber. A broken subscriber never takes
   *  the terminal down. */
  private emit(msg: TerminalMessage): void {
    for (const fn of this.subs) {
      try {
        fn(msg);
      } catch (err) {
        this.logger.warn("terminal subscriber failed", { detail: (err as Error).message });
      }
    }
  }

  /** Attach a subscriber; sends `ready` (+ the `exit` if the shell already ended,
   *  so a late attacher learns the terminal is dead). Returns an unsubscribe fn. */
  /** Attached client count — 0 means orphaned (the registry reaps it). */
  get subscriberCount(): number {
    return this.subs.size;
  }

  subscribe(fn: (m: TerminalMessage) => void): () => void {
    this.subs.add(fn);
    fn({ type: "ready", panelId: this.panelId, wire: TERMINAL_WIRE_VERSION, cols: this.cols, rows: this.rows });
    if (this.exited) fn({ type: "exit", code: this.exited.code, ...(this.exited.signal !== undefined ? { signal: this.exited.signal } : {}) });
    return () => {
      this.subs.delete(fn);
    };
  }

  /** Dispatch one client input into the pty. Never throws — a bad event is dropped. */
  dispatch(ev: TerminalInput): void {
    if (this.status === "closed") return;
    try {
      if (ev.type === "input" && typeof ev.data === "string") {
        this.pty.write(ev.data);
        return;
      }
      if (ev.type === "resize") {
        this.cols = clampCell(ev.cols, this.cols);
        this.rows = clampCell(ev.rows, this.rows);
        this.pty.resize(this.cols, this.rows);
        return;
      }
    } catch (err) {
      this.logger.warn("terminal input dispatch failed", { type: ev?.type, detail: (err as Error).message });
    }
  }

  /** Tear down: kill the pty (no orphan shell), tell subscribers. Idempotent. */
  close(reason = "closed"): void {
    if (this.status === "closed" && this.subs.size === 0 && !this.exited) return;
    const wasLive = this.status !== "closed";
    this.status = "closed";
    try {
      this.pty.kill();
    } catch {
      /* already gone */
    }
    if (wasLive) this.emit({ type: "exit", code: 0 });
    this.subs.clear();
    this.logger.info("terminal closed", { reason });
  }
}

export class TerminalAtCapacity extends Error {}
export class BadTerminalRequest extends Error {}

export interface OpenTerminalRequest {
  sessionId?: string | undefined;
  cols?: number | undefined;
  rows?: number | undefined;
  cwd?: string | undefined;
  shell?: string | undefined;
}

/**
 * The pod's live terminal table + provisioning. Ptys are featherweight, so the cap
 * is generous relative to browser panels, but still bounded so a runaway client
 * cannot fork-bomb the pod with shells.
 */
export class TerminalRegistry {
  private readonly terminals = new Map<string, TerminalSession>();

  /** Reclaim a terminal whose last client disconnected (see PanelRegistry) — a
   *  leaked pty is a live shell, so this matters more than a stray page. */
  private readonly orphanTimers = new Map<string, ReturnType<typeof setTimeout>>();

  noteDetached(panelId: string): void {
    const existing = this.orphanTimers.get(panelId);
    if (existing) clearTimeout(existing);
    const t = setTimeout(() => {
      this.orphanTimers.delete(panelId);
      const s = this.get(panelId);
      if (!s) return;
      if (s.subscriberCount > 0) return;      // re-attached
      log.info("terminal reaped (orphaned)", { panel_id: panelId });
      void this.close(panelId);
    }, this.orphanGraceMs);
    if (typeof t.unref === "function") t.unref();
    this.orphanTimers.set(panelId, t);
  }

  noteAttached(panelId: string): void {
    const t = this.orphanTimers.get(panelId);
    if (t) { clearTimeout(t); this.orphanTimers.delete(panelId); }
  }

  constructor(
    private readonly driver: TerminalDriver,
    private readonly maxTerminals = 16,
    /** The per-session sandbox ROOT — every pty's cwd is clamped under it. Defaults
     *  to the harness sandbox convention or the OS tmp when unset. */
    private readonly sandboxRoot: string = process.env.PANEL_SANDBOX_ROOT ??
      process.env.HARNESS_HOME ??
      join(process.env.TMPDIR ?? "/tmp", "glyphh-panel-terminals"),
    /** Grace period before an orphaned terminal (last client gone) is reaped —
     *  LAST so existing positional callers keep their argument order. */
    private readonly orphanGraceMs = 30_000,
  ) {}

  get(panelId: string): TerminalSession | undefined {
    return this.terminals.get(panelId);
  }

  count(): number {
    let n = 0;
    for (const t of this.terminals.values()) if (t.status !== "closed") n++;
    return n;
  }

  /**
   * Resolve the sandbox cwd for a terminal. The cwd is ALWAYS under the session's
   * sandbox dir — a client-supplied `cwd` is joined RELATIVE to the sandbox and
   * clamped so `../..` can never escape it. Absolute client paths are ignored.
   */
  private sandboxCwd(sessionId: string | undefined, requested: string | undefined): string {
    const sessionDir = resolve(this.sandboxRoot, "sessions", sanitizeSeg(sessionId || "default"), "workspace");
    if (!requested) return sessionDir;
    // Treat the request as relative, strip any leading separators/drive, then clamp.
    const rel = requested.replace(/^([A-Za-z]:)?[\\/]+/, "");
    const target = resolve(sessionDir, rel);
    // Must stay within the session dir (prefix check with a trailing separator).
    if (target === sessionDir || target.startsWith(sessionDir + sep)) return target;
    return sessionDir;
  }

  /** Provision a terminal: validate → capacity → ensure sandbox → spawn pty →
   *  register. Throws {@link TerminalAtCapacity}. */
  async open(req: OpenTerminalRequest): Promise<{ panelId: string; session: TerminalSession }> {
    if (this.count() >= this.maxTerminals) throw new TerminalAtCapacity("this pod is at its terminal capacity");
    const cols = clampCell(req.cols, 80);
    const rows = clampCell(req.rows, 24);
    const cwd = this.sandboxCwd(req.sessionId, req.cwd);
    try {
      await mkdir(cwd, { recursive: true });
    } catch (err) {
      throw new BadTerminalRequest(`could not create the sandbox dir: ${(err as Error).message}`);
    }
    const pty = await this.driver.spawn({ cwd, cols, rows, shell: req.shell });
    const panelId = mintTerminalId();
    const session = new TerminalSession({ panelId, pty, cols, rows, ...(req.sessionId ? { sessionId: req.sessionId } : {}) });
    this.terminals.set(panelId, session);
    log.info("terminal opened", { panel_id: panelId, session: req.sessionId || undefined, live: this.count() });
    return { panelId, session };
  }

  /** Close and forget one terminal (kills the pty). Returns false when unknown. */
  close(panelId: string): boolean {
    const session = this.terminals.get(panelId);
    if (!session) return false;
    this.terminals.delete(panelId);
    session.close("closed");
    return true;
  }

  /** Kill every pty (pod shutdown). No orphan shells. */
  async closeAll(): Promise<void> {
    for (const [id, session] of this.terminals) {
      this.terminals.delete(id);
      session.close("shutdown");
    }
    await this.driver.shutdown().catch(() => {});
  }
}

/** Sanitize a session id into a single safe path segment. */
function sanitizeSeg(s: string): string {
  const cleaned = s.replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 128);
  return cleaned || "default";
}
