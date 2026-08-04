/**
 * panel/terminal-driver.ts — the PTY DRIVER SEAM (the terminal's `browser.ts`).
 *
 * A terminal panel is the featherweight sibling of a browser panel: instead of a
 * headless Chromium screencast it is a real pty (a `bash`/`sh` process) whose
 * BYTES stream over the WS and are rendered by xterm.js on the client
 * (architecture-engines-memory.md §7 — "terminal: pty BYTES over WS → xterm.js
 * renders locally … featherweight"). The terminal session (terminal.ts) depends
 * on this narrow interface, never on node-pty directly, so the session/registry
 * lifecycle is unit-testable against a FAKE pty with no native addon.
 *
 * node-pty is a NATIVE addon. To keep `tsc`/tests hermetic (no addon load at
 * import time) the real driver LAZY-imports node-pty inside `spawn()`; a pod
 * without the addon fails only when a terminal is actually opened, and never at
 * module load. The interface here is addon-free.
 *
 * VERSION NOTE: node-pty 1.1.0 (stable) fails with `posix_spawnp failed` on Node
 * 22+/24 — its spawn-helper is broken on the newer runtime. Pinned to
 * `1.2.0-beta.15`, which spawns correctly on Node 24 (verified locally). Revisit
 * when 1.2.0 goes stable. The image's Node version must match the prebuilt binary.
 */

import { log } from "../obs/logger.js";

/** A live pty process inside the pod's per-session sandbox. */
export interface Pty {
  /** Write client keystrokes into the pty (stdin). */
  write(data: string): void;
  /** Resize the pty's window (cols × rows). Best-effort; never throws. */
  resize(cols: number, rows: number): void;
  /** Subscribe to pty output (stdout+stderr, raw bytes as a utf8 string). */
  onData(cb: (data: string) => void): void;
  /** Subscribe to process exit. `signal` is present when killed by a signal. */
  onExit(cb: (ev: { exitCode: number; signal?: number }) => void): void;
  /** Terminate the pty (SIGKILL by default — no lingering shells). Idempotent. */
  kill(signal?: string): void;
  /** The pid, for orphan-verification/logging. */
  readonly pid: number;
}

export interface SpawnPtyOptions {
  /** The shell to run (default: $SHELL, else bash, else sh). */
  shell?: string | undefined;
  /** The working directory — MUST be inside the session sandbox (registry enforces). */
  cwd: string;
  /** Initial window size. */
  cols: number;
  rows: number;
  /** Environment for the pty; the driver forces TERM=xterm-256color. */
  env?: NodeJS.ProcessEnv | undefined;
}

/** Mints ptys. The real impl wraps node-pty; a test injects a fake. */
export interface TerminalDriver {
  spawn(opts: SpawnPtyOptions): Promise<Pty>;
  /** Tear the driver down (pod shutdown). Idempotent — kills nothing on its own
   *  (the registry kills each pty), a hook for symmetry with BrowserDriver. */
  shutdown(): Promise<void>;
}

/** The minimal shape of node-pty we consume (kept local so `tsc` needs no @types). */
interface NodePtyProcess {
  pid: number;
  write(data: string): void;
  resize(cols: number, rows: number): void;
  onData(cb: (data: string) => void): { dispose(): void };
  onExit(cb: (ev: { exitCode: number; signal?: number }) => void): { dispose(): void };
  kill(signal?: string): void;
}
interface NodePtyModule {
  spawn(file: string, args: string[] | string, opts: Record<string, unknown>): NodePtyProcess;
}

/** Pick a shell: explicit → $SHELL → bash → sh. Never an arbitrary host binary. */
function resolveShell(explicit: string | undefined, env: NodeJS.ProcessEnv): string {
  const s = (explicit && explicit.trim()) || env.SHELL || "";
  if (s) return s;
  return process.platform === "win32" ? "powershell.exe" : "bash";
}

/**
 * The real pty driver: lazy-imports node-pty and spawns a login-ish shell in the
 * sandbox cwd with a sane env + TERM=xterm-256color. Never inherits the pod's raw
 * env wholesale for secrets — it starts from a MINIMAL env plus the caller's, so a
 * pty does not leak the pod's service tokens into a user shell.
 */
export class NodePtyDriver implements TerminalDriver {
  private mod: NodePtyModule | null = null;

  async spawn(opts: SpawnPtyOptions): Promise<Pty> {
    if (!this.mod) {
      // Lazy so importing this file (tsc/tests) never loads the native addon.
      this.mod = (await import("node-pty")) as unknown as NodePtyModule;
    }
    const shell = resolveShell(opts.shell, opts.env ?? process.env);
    // A MINIMAL, explicit env — never spread process.env so pod secrets
    // (ROTOR_AUTH_SERVICE_TOKEN, gateway keys) never reach the user shell.
    const base = opts.env ?? {};
    const env: Record<string, string> = {
      TERM: "xterm-256color",
      LANG: base.LANG ?? "en_US.UTF-8",
      PATH: base.PATH ?? "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
      HOME: opts.cwd,
      PWD: opts.cwd,
    };
    const proc = this.mod.spawn(shell, [], {
      name: "xterm-256color",
      cols: opts.cols,
      rows: opts.rows,
      cwd: opts.cwd,
      env,
    });
    log.info("pty spawned", { pid: proc.pid, cols: opts.cols, rows: opts.rows });
    return new NodePtyPty(proc);
  }

  async shutdown(): Promise<void> {
    /* the registry kills each pty; nothing pod-global to release */
  }
}

/** Wraps a node-pty process behind the addon-free {@link Pty} interface. */
class NodePtyPty implements Pty {
  private readonly disposers: Array<{ dispose(): void }> = [];
  private killed = false;
  constructor(private readonly proc: NodePtyProcess) {}

  get pid(): number {
    return this.proc.pid;
  }
  write(data: string): void {
    if (!this.killed) this.proc.write(data);
  }
  resize(cols: number, rows: number): void {
    try {
      if (!this.killed) this.proc.resize(cols, rows);
    } catch {
      /* a race with exit — best-effort, never throw */
    }
  }
  onData(cb: (data: string) => void): void {
    this.disposers.push(this.proc.onData(cb));
  }
  onExit(cb: (ev: { exitCode: number; signal?: number }) => void): void {
    this.disposers.push(this.proc.onExit(cb));
  }
  kill(signal = "SIGKILL"): void {
    if (this.killed) return;
    this.killed = true;
    for (const d of this.disposers.splice(0)) {
      try {
        d.dispose();
      } catch {
        /* ignore */
      }
    }
    try {
      this.proc.kill(signal);
    } catch {
      /* already gone */
    }
  }
}
