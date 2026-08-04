/**
 * panel/playwright-driver.ts — the real {@link BrowserDriver}, playwright-core +
 * headless Chromium.
 *
 * ONE Chromium process per pod (lazy-launched on the first panel), a fresh
 * BROWSER CONTEXT per panel for isolation (own cookies/storage/cache), one page
 * and one CDP session per context. playwright-core is chosen over puppeteer-core
 * because the runtime already vendors Chromium via playwright's browser cache
 * for its other tooling, its `newCDPSession` gives us the raw CDP channel the
 * spec locks us to (Page.startScreencast + Input.dispatch*), and it ships no
 * bundled browser download of its own (`-core`) so the image controls the binary.
 *
 * Container flags: `--no-sandbox` (no user-namespace sandbox in the pod — the
 * pod IS the isolation boundary) and `--disable-dev-shm-usage` (Chromium's
 * default /dev/shm is tiny in containers; force it to /tmp to avoid crashes).
 * These are the standard headless-Chromium-in-a-container flags.
 *
 * Secret hygiene: launch/nav diagnostics never log a full url with credentials
 * (redaction is applied by callers via frames.redactUrl); this module logs only
 * counts + the executable path.
 */

import { chromium } from "playwright-core";
import type { Browser, BrowserContext, CDPSession, Page } from "playwright-core";

import { log } from "../obs/logger.js";
import type { BrowserDriver, OpenPageOptions, PanelPage } from "./browser.js";

/** Chromium launch flags for a headless container. */
const CONTAINER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"];

class PlaywrightPage implements PanelPage {
  constructor(
    private readonly context: BrowserContext,
    private readonly page: Page,
    private readonly cdp: CDPSession,
  ) {}

  send(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    // playwright's CDPSession.send is typed to a protocol union; the panel uses a
    // dynamic method string, so this is the one `any` seam (matches the codebase's
    // "any only at open plugin seams" rule).
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

  async close(): Promise<void> {
    // Closing the context tears down the page, the CDP session, and all of the
    // panel's storage — no orphaned page or leaked cookies.
    try {
      await this.context.close();
    } catch (err) {
      log.warn("panel context close failed", { detail: (err as Error).message });
    }
  }
}

export class PlaywrightBrowserPool implements BrowserDriver {
  private browser: Browser | null = null;
  private launching: Promise<Browser> | null = null;

  constructor(private readonly executablePath?: string) {}

  /** Launch (once) or reuse the pod's single Chromium. Concurrent opens share the
   *  same in-flight launch so only one process starts. */
  private async ensureBrowser(): Promise<Browser> {
    if (this.browser?.isConnected()) return this.browser;
    if (this.launching) return this.launching;
    const t0 = Date.now();
    this.launching = chromium
      .launch({ headless: true, args: CONTAINER_ARGS, ...(this.executablePath ? { executablePath: this.executablePath } : {}) })
      .then((b) => {
        this.browser = b;
        this.launching = null;
        log.info("panel chromium launched", { cold_start_ms: Date.now() - t0 });
        b.on("disconnected", () => {
          if (this.browser === b) this.browser = null;
        });
        return b;
      })
      .catch((err) => {
        this.launching = null;
        throw err;
      });
    return this.launching;
  }

  async open(opts: OpenPageOptions): Promise<PanelPage> {
    const browser = await this.ensureBrowser();
    const context = await browser.newContext({ viewport: opts.viewport, deviceScaleFactor: opts.deviceScaleFactor ?? 1 });
    const page = await context.newPage();
    const cdp = await context.newCDPSession(page);
    return new PlaywrightPage(context, page, cdp);
  }

  async shutdown(): Promise<void> {
    const b = this.browser;
    this.browser = null;
    if (b?.isConnected()) {
      try {
        await b.close();
      } catch (err) {
        log.warn("panel browser shutdown failed", { detail: (err as Error).message });
      }
    }
  }
}
