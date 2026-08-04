/**
 * panel/driver-select.ts — pick the browser driver from what the machine can
 * actually do, once, lazily, at the first panel open.
 *
 * There are two drivers and the choice is not a preference, it is a capability:
 *
 *   Xvfb + ffmpeg + werift present  → {@link XvfbBrowserPool}: headful Chromium
 *                                     on a virtual display, native encode,
 *                                     WebRTC offered (and better bot-check
 *                                     behaviour, which is a real second win).
 *   anything missing                → {@link PlaywrightBrowserPool}: the v1
 *                                     headless pool, JPEG screencast only.
 *
 * The probe is deferred to the first `open()` rather than run at boot so
 * `startPanelServer()` stays synchronous (tests construct it directly) and so a
 * pod that never opens a browser panel never spawns a probe process. It runs
 * exactly once and is shared by every panel.
 *
 * `PANEL_XVFB` forces the outcome: `0` pins the headless driver (useful to
 * measure the v1 baseline on a machine that could do v2), `1` demands the
 * headful one and reports loudly if the prerequisites are missing rather than
 * silently degrading. The default is `auto`.
 */

import { log } from "../obs/logger.js";
import type { BrowserDriver, OpenPageOptions, PanelPage } from "./browser.js";
import { PlaywrightBrowserPool } from "./playwright-driver.js";
import { XvfbBrowserPool, screenFromEnv } from "./xvfb-driver.js";
import { hasXvfb } from "./xvfb.js";
import { webrtcAvailable } from "./webrtc.js";

interface Resolved {
  driver: BrowserDriver;
  webrtc: boolean;
  why: string;
}

/**
 * A {@link BrowserDriver} that decides which real driver it is on first use.
 *
 * It also answers the pod's "can this panel do WebRTC?" question, so the
 * capability and the driver that provides it can never disagree.
 */
export class AutoBrowserDriver implements BrowserDriver {
  private resolved: Promise<Resolved> | null = null;

  constructor(private readonly env: NodeJS.ProcessEnv) {}

  private resolve(): Promise<Resolved> {
    if (this.resolved) return this.resolved;
    this.resolved = (async (): Promise<Resolved> => {
      const mode = this.env.PANEL_XVFB ?? "auto";
      const chromium = this.env.PANEL_CHROMIUM_PATH;
      if (mode === "0") {
        log.info("panel driver: headless (PANEL_XVFB=0)", {});
        return { driver: new PlaywrightBrowserPool(chromium), webrtc: false, why: "pinned headless" };
      }
      const [xvfb, rtc] = await Promise.all([hasXvfb(this.env.PANEL_XVFB_PATH), webrtcAvailable(this.env.PANEL_FFMPEG_PATH)]);
      if (xvfb && rtc) {
        log.info("panel driver: headful on Xvfb, webrtc available", {});
        return {
          driver: new XvfbBrowserPool({
            executablePath: chromium,
            xvfbPath: this.env.PANEL_XVFB_PATH,
            screen: screenFromEnv(this.env),
          }),
          webrtc: true,
          why: "xvfb + ffmpeg + werift present",
        };
      }
      const why = `${xvfb ? "" : "no Xvfb; "}${rtc ? "" : "no ffmpeg/werift; "}`.replace(/; $/, "");
      const level = mode === "1" ? "error" : "info";
      log[level]("panel driver: headless — webrtc unavailable", { detail: why });
      return { driver: new PlaywrightBrowserPool(chromium), webrtc: false, why };
    })();
    return this.resolved;
  }

  async open(opts: OpenPageOptions): Promise<PanelPage> {
    const { driver } = await this.resolve();
    return driver.open(opts);
  }

  async supportsWebrtc(): Promise<boolean> {
    const { webrtc } = await this.resolve();
    return webrtc;
  }

  async shutdown(): Promise<void> {
    // Never force the probe just to shut down a pod that opened nothing.
    if (!this.resolved) return;
    const { driver } = await this.resolved;
    await driver.shutdown();
  }
}
