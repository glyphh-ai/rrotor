/**
 * panel/xvfb.ts — a virtual X display per panel, so Chromium can run HEADFUL.
 *
 * Two things the v1 headless panel could not do, both fixed by the same move:
 *
 *  1. **Capture.** `ffmpeg -f x11grab` needs a real X display to read. Headless
 *     Chromium has none, which is why v1 had to pull pixels back through CDP as
 *     JPEG — the expensive path this spike exists to replace.
 *  2. **Bot detection.** `headless:true` is a fingerprint. Cloudflare/Turnstile
 *     treat headless + datacenter IP as hostile and will hard-challenge it.
 *     Headful Chromium on a virtual display is a real browser by every signal a
 *     page can read — same renderer, real window, real compositor, working
 *     WebGL (SwiftShader) — while still running in a container with no GPU and
 *     no monitor.
 *
 * One Xvfb per panel, not one per pod. It costs a few MB of framebuffer, and it
 * buys the thing that makes the encoder trivial: the panel's window is the ONLY
 * window on its display, at a position we choose, so the capture rect is
 * arithmetic instead of window-manager archaeology. It also means one panel can
 * never capture another panel's pixels — the isolation §7b asks for, enforced by
 * the X server rather than by our own care.
 *
 * The framebuffer is allocated GENEROUSLY once (PANEL_XVFB_SCREEN) rather than
 * resized: Xvfb cannot be resized without RandR gymnastics, but a window inside
 * an oversized framebuffer resizes freely, and the encoder just grabs a
 * different rect. So a client resize costs an ffmpeg restart, not a display
 * restart.
 *
 * Secret hygiene: nothing here touches a url or a token; logs carry the display
 * number and geometry only.
 */

import { spawn } from "node:child_process";
import type { ChildProcess } from "node:child_process";
import * as fs from "node:fs";

import { log } from "../obs/logger.js";

/** The X11 socket directory Xvfb creates its socket in — the readiness signal. */
const X11_SOCKET_DIR = "/tmp/.X11-unix";

/** Display numbers are pod-global; start well above :0 so we never collide with
 *  a real session, and above the range a developer's X server would use. */
const FIRST_DISPLAY = 90;
const LAST_DISPLAY = 200;

const claimed = new Set<number>();

/** Parse a `WxH` screen geometry, falling back to the default on nonsense. */
export function parseScreen(raw: string | undefined, fallback: { width: number; height: number }): { width: number; height: number } {
  const m = /^(\d{2,5})x(\d{2,5})$/.exec((raw ?? "").trim());
  if (!m) return fallback;
  const width = Number(m[1]);
  const height = Number(m[2]);
  if (!Number.isFinite(width) || !Number.isFinite(height) || width < 320 || height < 240) return fallback;
  return { width, height };
}

/** The default virtual framebuffer. Big enough for a 1280×800 CSS viewport at
 *  DPR 2 plus browser chrome; ~12MB of framebuffer at 24bpp, which is noise next
 *  to Chromium. */
export const DEFAULT_SCREEN = { width: 2560, height: 1700 };

export interface XvfbOptions {
  screen?: { width: number; height: number };
  /** Xvfb binary; defaults to `Xvfb` on PATH. */
  xvfbPath?: string;
  logFields?: Record<string, unknown>;
}

/** One running Xvfb, owning exactly one display number. */
export class XvfbDisplay {
  readonly display: string;
  readonly screen: { width: number; height: number };
  private proc: ChildProcess | null = null;
  private stopped = false;
  private readonly num: number;
  private readonly logger;

  private constructor(num: number, screen: { width: number; height: number }, logFields?: Record<string, unknown>) {
    this.num = num;
    this.display = `:${num}`;
    this.screen = screen;
    this.logger = log.child({ ...(logFields ?? {}), display: this.display });
  }

  /** Claim a free display number, spawn Xvfb, and wait for its socket. Throws if
   *  Xvfb is missing or never becomes ready — the caller falls back to headless
   *  (screencast-only), it does not fail the panel. */
  static async start(opts: XvfbOptions = {}): Promise<XvfbDisplay> {
    const screen = opts.screen ?? DEFAULT_SCREEN;
    const num = claimDisplay();
    const inst = new XvfbDisplay(num, screen, opts.logFields);
    try {
      await inst.spawn(opts.xvfbPath);
      return inst;
    } catch (err) {
      claimed.delete(num);
      throw err;
    }
  }

  private async spawn(xvfbPath?: string): Promise<void> {
    const args = [
      this.display,
      "-screen", "0", `${this.screen.width}x${this.screen.height}x24`,
      // No TCP listener: the display is reachable only over its unix socket,
      // inside this container. Nothing outside the pod can ever attach to it.
      "-nolisten", "tcp",
      // Survive the last client disconnecting (a Chromium crash must not take the
      // display with it and strand the panel mid-restart).
      "-noreset",
      "-dpi", "96",
    ];
    const proc = spawn(xvfbPath || "Xvfb", args, { stdio: ["ignore", "ignore", "pipe"] });
    this.proc = proc;
    let spawnError: Error | null = null;
    proc.on("error", (err) => { spawnError = err; });
    proc.stderr?.setEncoding("utf8");
    proc.stderr?.on("data", (chunk: string) => {
      const line = chunk.trim();
      // Xvfb is chatty on stderr even when healthy; only surface real errors.
      if (line && /error|fatal|cannot/i.test(line)) this.logger.warn("xvfb stderr", { detail: line.slice(0, 300) });
    });
    proc.on("exit", (code, signal) => {
      this.proc = null;
      if (!this.stopped) this.logger.warn("xvfb exited unexpectedly", { code, signal });
    });

    const socket = `${X11_SOCKET_DIR}/X${this.num}`;
    const deadline = Date.now() + 8000;
    while (Date.now() < deadline) {
      if (spawnError) throw new Error(`Xvfb could not start: ${(spawnError as Error).message}`);
      if (proc.exitCode !== null) throw new Error(`Xvfb exited immediately (code=${proc.exitCode})`);
      if (fs.existsSync(socket)) {
        this.logger.info("xvfb ready", { screen: `${this.screen.width}x${this.screen.height}` });
        return;
      }
      await sleep(50);
    }
    await this.stop();
    throw new Error("Xvfb did not become ready within 8s");
  }

  /** Kill the display and release its number. Idempotent; never throws. */
  async stop(): Promise<void> {
    if (this.stopped) return;
    this.stopped = true;
    claimed.delete(this.num);
    const proc = this.proc;
    this.proc = null;
    if (proc && proc.exitCode === null) {
      try {
        proc.kill("SIGTERM");
        await new Promise<void>((resolve) => {
          const t = setTimeout(() => { try { proc.kill("SIGKILL"); } catch { /* gone */ } resolve(); }, 2000);
          proc.once("exit", () => { clearTimeout(t); resolve(); });
        });
      } catch {
        /* already dead */
      }
    }
    this.logger.info("xvfb stopped", {});
  }
}

function claimDisplay(): number {
  for (let n = FIRST_DISPLAY; n <= LAST_DISPLAY; n++) {
    if (claimed.has(n)) continue;
    // An existing socket means someone else's X server owns this number.
    if (fs.existsSync(`${X11_SOCKET_DIR}/X${n}`)) continue;
    claimed.add(n);
    return n;
  }
  throw new Error("no free X display number for a panel");
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

/** Whether an Xvfb binary exists and runs. Cached like the ffmpeg probe. */
let xvfbProbe: Promise<boolean> | null = null;

export function hasXvfb(xvfbPath?: string): Promise<boolean> {
  if (xvfbProbe) return xvfbProbe;
  xvfbProbe = new Promise<boolean>((resolve) => {
    try {
      // `Xvfb -help` exits non-zero on some builds, so treat "it ran at all" as
      // present: an ENOENT `error` event is the only reliable absence signal.
      const proc = spawn(xvfbPath || "Xvfb", ["-help"], { stdio: "ignore" });
      proc.on("error", () => resolve(false));
      proc.on("exit", () => resolve(true));
    } catch {
      resolve(false);
    }
  });
  return xvfbProbe;
}

/** Test seam — forget the cached Xvfb probe. */
export function resetXvfbProbe(): void {
  xvfbProbe = null;
}
