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

export interface OpenPanelRequest {
  url: string;
  sessionId?: string;
  viewport?: { width?: number; height?: number };
  quality?: number;
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

  constructor(
    private readonly driver: BrowserDriver,
    private readonly maxPanels = 4,
  ) {}

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

    const page = await this.driver.open({ viewport });
    const panelId = mintPanelId();
    const session = new PanelSession({ panelId, page, viewport, ...(req.sessionId ? { sessionId: req.sessionId } : {}), ...(req.quality ? { quality: req.quality } : {}) });
    try {
      await page.goto(url);
      await session.start();
    } catch (err) {
      // Provision failed after the page opened — tear it down, don't register.
      await session.close("provision-failed").catch(() => {});
      throw err;
    }
    this.panels.set(panelId, session);
    log.info("panel opened", { panel_id: panelId, session: req.sessionId || undefined, live: this.count() });
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
