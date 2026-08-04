/**
 * panel/xvfb-driver.ts — the HEADFUL {@link BrowserDriver}: one Xvfb + one real
 * Chromium window per panel, whose content rect the native encoder grabs.
 *
 * This is the driver that makes the v2 video path possible, and it trades one
 * property of the v1 driver away on purpose:
 *
 *   v1 (playwright-driver.ts)  ONE headless Chromium per pod, a browser CONTEXT
 *                              per panel. Cheap per panel; no display; pixels
 *                              can only leave through CDP as JPEG.
 *   v2 (this file)             ONE Xvfb + ONE headful Chromium per panel. More
 *                              memory per panel; a real display; pixels leave
 *                              through ffmpeg, encoded in native code.
 *
 * Why a whole browser per panel rather than tiled windows on one shared display:
 * a single window alone on its own display has a content rect that is pure
 * arithmetic, cannot be occluded, cannot be raised over by a neighbour, and
 * cannot be captured by another tenant's encoder. Tiling N panels onto one Xvfb
 * would save memory and cost all three of those. The density hit is real and is
 * reported as a measured number, not hidden.
 *
 * ── the capture rect ────────────────────────────────────────────────────────
 * Chromium draws its own toolbar, and there is no window manager on the display,
 * so the page content sits at a fixed offset inside the window we positioned at
 * the origin. The offset is derived ONLY from differences measured inside the
 * page — `outerHeight - innerHeight`, `(outerWidth - innerWidth)/2` — which are
 * reliable regardless of how the platform defines `screenX`, and from the window
 * position we set ourselves. It is re-measured after every resize.
 *
 * ── bot detection ───────────────────────────────────────────────────────────
 * Headful is half the answer; the flags are the other half. We do NOT pass
 * `--disable-gpu` (a page that finds WebGL missing knows something is wrong) —
 * SwiftShader gives a real, software-backed WebGL context. `--enable-automation`
 * is dropped and `AutomationControlled` blink features are disabled so
 * `navigator.webdriver` and the automation infobar go away. Combined with a real
 * window and a real compositor, the page-visible fingerprint of this browser is
 * an ordinary desktop Chrome.
 *
 * Secret hygiene: launch diagnostics log the display, geometry and cold-start
 * time — never a url, never the user data dir contents.
 */

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { chromium } from "playwright-core";
import type { BrowserContext, CDPSession, Page } from "playwright-core";

import { log } from "../obs/logger.js";
import type { BrowserDriver, OpenPageOptions, PanelPage } from "./browser.js";
import type { CaptureTarget } from "./encoder.js";
import { XvfbDisplay, DEFAULT_SCREEN, parseScreen } from "./xvfb.js";

/**
 * Chromium flags for a headful container browser.
 *
 * `--no-sandbox` / `--disable-dev-shm-usage` are the standard container pair (the
 * pod is the isolation boundary; the default /dev/shm is too small for heavy
 * pages). Everything after them is either window geometry we depend on or
 * fingerprint hygiene — see the header.
 */
export const HEADFUL_ARGS: readonly string[] = [
  "--no-sandbox",
  "--disable-dev-shm-usage",
  "--window-position=0,0",
  "--no-first-run",
  "--no-default-browser-check",
  "--disable-session-crashed-bubble",
  "--disable-infobars",
  // `--disable-infobars` no longer suppresses the "you are using an unsupported
  // command-line flag" bar that `--no-sandbox` triggers; `--test-type` does. It
  // is not page-visible, and without it every panel streams a yellow warning
  // strip across the top of the user's page.
  "--test-type",
  "--disable-features=Translate,MediaRouter",
  // A real (software) WebGL/GPU stack rather than none — see "bot detection".
  "--use-gl=angle",
  "--use-angle=swiftshader",
  "--enable-unsafe-swiftshader",
  // Remove the automation tells a page can read.
  "--disable-blink-features=AutomationControlled",
];

/** Playwright's own default args that would re-introduce an automation signal. */
const IGNORE_DEFAULT_ARGS: readonly string[] = ["--enable-automation", "--disable-extensions"];

/** The geometry the page reports about itself, all in CSS px. */
interface WindowGeometry {
  outerWidth: number;
  outerHeight: number;
  innerWidth: number;
  innerHeight: number;
  devicePixelRatio: number;
}

const GEOMETRY_EXPR = `({
  outerWidth: window.outerWidth, outerHeight: window.outerHeight,
  innerWidth: window.innerWidth, innerHeight: window.innerHeight,
  devicePixelRatio: window.devicePixelRatio,
})`;

class XvfbPage implements PanelPage {
  private closed = false;

  constructor(
    private readonly context: BrowserContext,
    private readonly page: Page,
    private readonly cdp: CDPSession,
    private readonly display: XvfbDisplay,
    private readonly userDataDir: string,
    private readonly logger: ReturnType<typeof log.child>,
  ) {}

  send(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    return (this.cdp.send as (m: string, p?: Record<string, unknown>) => Promise<unknown>)(method, params);
  }

  on(event: string, handler: (payload: any) => void): () => void {
    (this.cdp.on as (e: string, h: (p: any) => void) => void)(event, handler);
    return () => (this.cdp.off as (e: string, h: (p: any) => void) => void)(event, handler);
  }

  async goto(url: string): Promise<void> {
    await this.page.goto(url, { waitUntil: "domcontentloaded", timeout: 30_000 });
  }

  url(): string {
    return this.page.url();
  }

  async title(): Promise<string> {
    try {
      return await this.page.title();
    } catch {
      return "";
    }
  }

  /** The page's content rect on its display, in physical pixels. Re-measured on
   *  every call so a resize needs no bookkeeping — the encoder simply asks
   *  again. Returns null (→ screencast fallback) if the page cannot be evaluated
   *  right now, e.g. mid-navigation or already closed. */
  async capture(): Promise<CaptureTarget | null> {
    if (this.closed) return null;
    try {
      const g = (await this.page.evaluate(GEOMETRY_EXPR)) as WindowGeometry;
      if (!g || !Number.isFinite(g.innerWidth) || g.innerWidth < 2 || g.innerHeight < 2) return null;
      const dpr = g.devicePixelRatio > 0 ? g.devicePixelRatio : 1;
      // Chrome draws its toolbar at the top; side borders are 0 on Linux but the
      // symmetric-border form costs nothing and is correct if that ever changes.
      const borderX = Math.max(0, (g.outerWidth - g.innerWidth) / 2);
      const chromeY = Math.max(0, g.outerHeight - g.innerHeight);
      return {
        display: this.display.display,
        x: Math.round(borderX * dpr),
        y: Math.round(chromeY * dpr),
        width: Math.round(g.innerWidth * dpr),
        height: Math.round(g.innerHeight * dpr),
      };
    } catch (err) {
      this.logger.warn("panel capture rect unavailable", { detail: (err as Error).message });
      return null;
    }
  }

  /** Grow/shrink the real OS window so the CONTENT area becomes the requested
   *  CSS viewport. The encoder re-reads {@link capture} afterwards, so nothing
   *  has to be kept in sync by hand. */
  async resize(width: number, height: number): Promise<void> {
    if (this.closed) return;
    await fitContentToViewport(this.page, this.cdp, { width, height }, this.logger);
  }

  /** Tear the panel down completely: the browser, its display, and its profile
   *  directory. A leaked Xvfb or profile dir would outlive the pod's memory
   *  budget, so all three are unconditional. */
  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    try {
      await this.context.close();
    } catch (err) {
      this.logger.warn("panel context close failed", { detail: (err as Error).message });
    }
    await this.display.stop();
    try {
      fs.rmSync(this.userDataDir, { recursive: true, force: true });
    } catch (err) {
      this.logger.warn("panel profile cleanup failed", { detail: (err as Error).message });
    }
  }
}

export interface XvfbBrowserPoolOptions {
  executablePath?: string | undefined;
  xvfbPath?: string | undefined;
  /** Virtual framebuffer size, `WxH` (PANEL_XVFB_SCREEN). */
  screen?: { width: number; height: number };
}

/**
 * The headful driver. There is no pod-wide browser to pool — each `open()`
 * stands up a display + a Chromium and each `close()` tears both down — so
 * `shutdown()` only has to account for pages that were never closed.
 */
export class XvfbBrowserPool implements BrowserDriver {
  private readonly live = new Set<XvfbPage>();

  constructor(private readonly opts: XvfbBrowserPoolOptions = {}) {}

  async open(opts: OpenPageOptions): Promise<PanelPage> {
    const dpr = Math.min(3, Math.max(1, opts.deviceScaleFactor ?? 1));
    const screen = this.opts.screen ?? DEFAULT_SCREEN;
    const t0 = Date.now();
    const display = await XvfbDisplay.start({
      screen,
      ...(this.opts.xvfbPath ? { xvfbPath: this.opts.xvfbPath } : {}),
    });
    const logger = log.child({ display: display.display });
    const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "glyphh-panel-"));

    let context: BrowserContext;
    try {
      context = await chromium.launchPersistentContext(userDataDir, {
        headless: false,
        // viewport:null hands the page the REAL window's inner size. Anything
        // else would emulate device metrics, and the emulated size and the
        // window we are capturing would silently disagree.
        viewport: null,
        args: [...HEADFUL_ARGS, `--window-size=${Math.round(opts.viewport.width)},${Math.round(opts.viewport.height)}`, `--force-device-scale-factor=${dpr}`],
        ignoreDefaultArgs: [...IGNORE_DEFAULT_ARGS],
        env: { ...process.env, DISPLAY: display.display },
        ...(this.opts.executablePath ? { executablePath: this.opts.executablePath } : {}),
      });
    } catch (err) {
      await display.stop();
      fs.rmSync(userDataDir, { recursive: true, force: true });
      throw err;
    }

    try {
      const page = context.pages()[0] ?? (await context.newPage());
      const cdp = await context.newCDPSession(page);
      // The window was sized to the OUTER frame; grow it so the CONTENT is the
      // requested viewport, then the capture rect and the client's coordinate
      // space agree exactly.
      await fitContentToViewport(page, cdp, opts.viewport, logger);
      await forceFocus(cdp, logger);
      const panelPage = new XvfbPage(context, page, cdp, display, userDataDir, logger);
      this.live.add(panelPage);
      logger.info("panel headful chromium launched", {
        cold_start_ms: Date.now() - t0,
        viewport: `${opts.viewport.width}x${opts.viewport.height}`,
        dpr,
      });
      return panelPage;
    } catch (err) {
      await context.close().catch(() => {});
      await display.stop();
      fs.rmSync(userDataDir, { recursive: true, force: true });
      throw err;
    }
  }

  async shutdown(): Promise<void> {
    for (const page of [...this.live]) {
      this.live.delete(page);
      await page.close().catch(() => {});
    }
  }
}

/**
 * Resize the OS window so the page's content area is exactly the requested CSS
 * viewport, compensating for whatever chrome this Chromium build draws. Measure,
 * correct, measure again — a single pass is enough because the correction is a
 * constant, and the second read is what the log reports.
 */
async function fitContentToViewport(
  page: Page,
  cdp: CDPSession,
  viewport: { width: number; height: number },
  logger: ReturnType<typeof log.child>,
): Promise<void> {
  try {
    const g = (await page.evaluate(GEOMETRY_EXPR)) as WindowGeometry;
    const chromeH = Math.max(0, g.outerHeight - g.innerHeight);
    const chromeW = Math.max(0, g.outerWidth - g.innerWidth);
    // BOUND, not extracted: playwright's CDPSession.send reaches for `this._channel`,
    // so a detached method reference throws before the command is ever sent.
    const send = (cdp.send as (m: string, p?: Record<string, unknown>) => Promise<any>).bind(cdp);
    const { windowId } = (await send("Browser.getWindowForTarget", {})) as { windowId: number };
    await send("Browser.setWindowBounds", {
      windowId,
      bounds: {
        left: 0,
        top: 0,
        width: Math.round(viewport.width + chromeW),
        height: Math.round(viewport.height + chromeH),
        windowState: "normal",
      },
    });
    const after = (await page.evaluate(GEOMETRY_EXPR)) as WindowGeometry;
    logger.info("panel window fitted", {
      chrome_px: chromeH,
      content: `${after.innerWidth}x${after.innerHeight}`,
      wanted: `${viewport.width}x${viewport.height}`,
    });
  } catch (err) {
    // A window that will not resize is not fatal — the capture rect is measured
    // from the window we actually got, so the stream stays correct, just not the
    // exact size asked for.
    logger.warn("panel window fit failed", { detail: (err as Error).message });
  }
}

/**
 * Make the page believe it is focused, unconditionally.
 *
 * There is NO window manager on the Xvfb display, so nothing ever gives the
 * Chromium window X input focus. Chromium can then decline to move DOM focus on
 * a synthesized click, and `document.hasFocus()` reads false — which sites use
 * to pause work, suppress carets and skip autofocus. A panel where clicking a
 * text field does not produce a caret is not an interactive surface at all, and
 * the failure is invisible in a screenshot.
 *
 * `Emulation.setFocusEmulationEnabled` pins the page to focused. Running a real
 * window manager in the container would too, at the cost of another process per
 * panel. (Verified: with this on, a click reports `target=INPUT`, DOM focus
 * moves, and typed characters land in the field.)
 */
async function forceFocus(cdp: CDPSession, logger: ReturnType<typeof log.child>): Promise<void> {
  try {
    const send = (cdp.send as (m: string, p?: Record<string, unknown>) => Promise<any>).bind(cdp);
    await send("Emulation.setFocusEmulationEnabled", { enabled: true });
  } catch (err) {
    logger.warn("panel focus emulation failed — clicks may not focus fields", { detail: (err as Error).message });
  }
}

/** Resolve the framebuffer size from env (`PANEL_XVFB_SCREEN=2560x1700`). */
export function screenFromEnv(env: NodeJS.ProcessEnv): { width: number; height: number } {
  return parseScreen(env.PANEL_XVFB_SCREEN, DEFAULT_SCREEN);
}
