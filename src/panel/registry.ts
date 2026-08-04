/**
 * panel/registry.ts — the pod's live panel table + provisioning.
 *
 * Owns the map of open {@link PanelSession}s and the one seam that turns a
 * `POST /panel/browser` request into a running panel: launch a page on the
 * {@link BrowserDriver}, navigate it, construct the session, start its
 * screencast. A capacity cap (PANEL_MAX, default a small number — browser
 * panels are heavy) refuses new panels past the limit rather than OOM the pod.
 *
 * Structured so ONE panel is fine for the spike but multi-panel is not
 * precluded: the table is a map, each panel has its own isolated context, and
 * teardown is per-panel. `closeAll()` (pod shutdown) tears every panel + the
 * browser down so no Chromium is orphaned.
 */

import { log } from "../obs/logger.js";
import type { BrowserDriver } from "./browser.js";
import { clampDim } from "./input.js";
import { PanelSession, mintPanelId } from "./session.js";
import type { WebrtcConfig } from "./webrtc.js";

export interface OpenPanelRequest {
  url: string;
  sessionId?: string;
  viewport?: { width?: number; height?: number };
  quality?: number;
  /** Client devicePixelRatio (1–3) — renders at physical resolution. */
  deviceScaleFactor?: number;
}

export interface OpenPanelResult {
  panelId: string;
  session: PanelSession;
}

export class PanelAtCapacity extends Error {}
export class BadPanelRequest extends Error {}

const DEFAULT_VIEWPORT = { width: 1024, height: 720 };

export class PanelRegistry {
  private readonly panels = new Map<string, PanelSession>();

  /** Reclaim a panel whose last client disconnected. A reload drops the WS for a
   *  moment, so we wait out a grace period before closing — long enough to survive a
   *  refresh, short enough that an abandoned panel (tab closed, client crashed) never
   *  pins a Chromium page and wedges the pod at capacity. */
  private readonly orphanTimers = new Map<string, ReturnType<typeof setTimeout>>();

  constructor(
    private readonly driver: BrowserDriver,
    private readonly maxPanels = 4,
    private readonly orphanGraceMs = 30_000,
    /** WebRTC settings for every panel this registry provisions. Absent → the
     *  pod is screencast-only, which is also what happens when the driver says
     *  it cannot support WebRTC. */
    private readonly webrtc?: WebrtcConfig,
  ) {}

  /** Called when a client detaches: start (or restart) the orphan countdown. A
   *  re-attach cancels it. */
  noteDetached(panelId: string): void {
    const existing = this.orphanTimers.get(panelId);
    if (existing) clearTimeout(existing);
    const t = setTimeout(() => {
      this.orphanTimers.delete(panelId);
      const p = this.panels.get(panelId);
      if (!p || p.status === "closed") return;
      if (p.subscriberCount > 0) return;          // someone re-attached
      log.info("panel reaped (orphaned)", { panel_id: panelId });
      void this.close(panelId).catch(() => { /* already gone */ });
    }, this.orphanGraceMs);
    if (typeof t.unref === "function") t.unref();
    this.orphanTimers.set(panelId, t);
  }

  /** Called when a client attaches: cancel any pending orphan reap. */
  noteAttached(panelId: string): void {
    const t = this.orphanTimers.get(panelId);
    if (t) { clearTimeout(t); this.orphanTimers.delete(panelId); }
  }

  get(panelId: string): PanelSession | undefined {
    return this.panels.get(panelId);
  }

  count(): number {
    let n = 0;
    for (const p of this.panels.values()) if (p.status !== "closed") n++;
    return n;
  }

  /**
   * Provision a panel: validate → capacity check → launch page → navigate →
   * start screencast → register. Throws {@link BadPanelRequest} (→400) or
   * {@link PanelAtCapacity} (→409). On any failure after the page opens, the
   * page is torn down so a failed provision leaks nothing.
   */
  async open(req: OpenPanelRequest): Promise<OpenPanelResult> {
    const url = typeof req.url === "string" ? req.url.trim() : "";
    if (!/^https?:\/\//i.test(url)) throw new BadPanelRequest("`url` must be an http(s) URL");
    if (this.count() >= this.maxPanels) throw new PanelAtCapacity("this pod is at its browser-panel capacity");

    const viewport = {
      width: clampDim(req.viewport?.width, DEFAULT_VIEWPORT.width),
      height: clampDim(req.viewport?.height, DEFAULT_VIEWPORT.height),
    };

    const dpr = Math.min(3, Math.max(1, Number(req.deviceScaleFactor) || 1));
    // Ask the driver, not the environment: the driver IS the capability (it is
    // the thing that produces capturable pages), so the two can never disagree.
    const webrtcAvailable = this.webrtc !== undefined && this.driver.supportsWebrtc !== undefined && (await this.driver.supportsWebrtc());
    const page = await this.driver.open({ viewport, deviceScaleFactor: dpr });
    const panelId = mintPanelId();
    const session = new PanelSession({
      panelId,
      page,
      viewport,
      deviceScaleFactor: dpr,
      webrtcAvailable,
      ...(this.webrtc ? { webrtc: this.webrtc } : {}),
      ...(req.sessionId ? { sessionId: req.sessionId } : {}),
      ...(req.quality ? { quality: req.quality } : {}),
    });
    try {
      await page.goto(url);
      await session.start();
    } catch (err) {
      // Provision failed after the page opened — tear it down, don't register.
      await session.close("provision-failed").catch(() => {});
      throw err;
    }
    this.panels.set(panelId, session);
    log.info("panel opened", {
      panel_id: panelId,
      session: req.sessionId || undefined,
      live: this.count(),
      webrtc: session.capabilities().webrtc,
    });
    return { panelId, session };
  }

  /** Close and forget one panel. Returns false when unknown. */
  async close(panelId: string): Promise<boolean> {
    const session = this.panels.get(panelId);
    if (!session) return false;
    this.panels.delete(panelId);
    await session.close("closed").catch(() => {});
    return true;
  }

  /** Tear every panel + the browser down (pod shutdown). No orphan Chromium. */
  async closeAll(): Promise<void> {
    for (const [id, session] of this.panels) {
      this.panels.delete(id);
      await session.close("shutdown").catch(() => {});
    }
    await this.driver.shutdown().catch(() => {});
  }
}
