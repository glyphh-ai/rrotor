/**
 * panel/session.ts — ONE browser panel: a real page in the pod, its screencast
 * fanned out to attached clients, and client input dispatched back into it.
 *
 * A `PanelSession` owns a {@link PanelPage} (an isolated browser context) and:
 *   • starts a CDP screencast → each `Page.screencastFrame` becomes a `frame`
 *     wire message, ACKed per protocol (mandatory — Chromium stalls the
 *     screencast until the frame is ACKed), and fanned out to every subscriber.
 *   • subscribes `Page.frameNavigated` → emits a `nav` message on url/title
 *     change so the client's address bar tracks the real page.
 *   • takes {@link InputEvent}s off the WS, maps them via panel/input.ts, and
 *     dispatches the CDP command; a `resize` re-emulates device metrics AND
 *     restarts the screencast at the new size.
 *   • tears the context down on `close()` — no orphan Chromium page/context.
 *
 * Screencast is LIVE-ONLY (no replay ring, unlike the harness): a late/re-
 * attaching subscriber receives the next keyframe on the next visual change.
 * frames.ts documents why.
 *
 * The session is transport-agnostic: it fans `PanelMessage`s to subscriber
 * callbacks. The WS server (server.ts) is one subscriber; a test is another.
 */

import { log } from "../obs/logger.js";
import type { PanelPage } from "./browser.js";
import { PANEL_WIRE_VERSION, redactUrl } from "./frames.js";
import type { PanelMessage } from "./frames.js";
import { toCdp, resizeMetrics, clampDim } from "./input.js";
import type { InputEvent } from "./input.js";

export type PanelStatus = "starting" | "live" | "closed";

/** A raw CDP `Page.screencastFrame` payload (the fields we use). */
interface ScreencastFramePayload {
  data: string;
  sessionId: number;
  metadata?: { deviceWidth?: number; deviceHeight?: number };
}

export interface PanelSessionOptions {
  panelId: string;
  page: PanelPage;
  viewport: { width: number; height: number };
  /** Screencast image quality 0–100 (jpeg). Default 60. */
  quality?: number;
  /** Injectable clock for deterministic `at` stamps in tests. */
  now?: () => number;
}

/** Mint a panel id. */
export function mintPanelId(): string {
  return `pnl-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}`;
}

export class PanelSession {
  readonly panelId: string;
  readonly sessionId: string;
  status: PanelStatus = "starting";

  private readonly page: PanelPage;
  private viewport: { width: number; height: number };
  private readonly quality: number;
  private readonly now: () => number;
  private readonly logger;

  private readonly subs = new Set<(m: PanelMessage) => void>();
  private readonly unsubscribers: Array<() => void> = [];
  private lastNav = "";
  private lastTitle = "";

  constructor(opts: PanelSessionOptions & { sessionId?: string }) {
    this.panelId = opts.panelId;
    this.sessionId = opts.sessionId ?? "";
    this.page = opts.page;
    this.viewport = { width: clampDim(opts.viewport.width, 1024), height: clampDim(opts.viewport.height, 720) };
    this.quality = Math.min(100, Math.max(1, opts.quality ?? 60));
    this.now = opts.now ?? Date.now;
    this.logger = log.child({ panel_id: this.panelId });
  }

  /** Wire CDP events and start the screencast. Call once, after the page has
   *  navigated to its initial url. */
  async start(): Promise<void> {
    // Screencast frames → fan out + ACK (ACK is mandatory or Chromium stalls).
    this.unsubscribers.push(
      this.page.on("Page.screencastFrame", (p: ScreencastFramePayload) => {
        this.emit({
          type: "frame",
          data: p.data,
          format: "jpeg",
          meta: { deviceWidth: p.metadata?.deviceWidth ?? this.viewport.width, deviceHeight: p.metadata?.deviceHeight ?? this.viewport.height },
          at: this.now(),
        });
        void this.page.send("Page.screencastFrameAck", { sessionId: p.sessionId }).catch(() => {
          /* a dead session's ACK failing is expected on teardown */
        });
      }),
    );
    // Navigation → nav message (url/title). frameNavigated fires for subframes
    // too; we only report when the top-level url actually changed.
    this.unsubscribers.push(
      this.page.on("Page.frameNavigated", () => {
        void this.reportNav();
      }),
    );

    await this.startScreencast();
    this.status = "live";
    await this.reportNav();
  }

  private async startScreencast(): Promise<void> {
    await this.page.send("Page.startScreencast", {
      format: "jpeg",
      quality: this.quality,
      maxWidth: this.viewport.width,
      maxHeight: this.viewport.height,
      everyNthFrame: 1,
    });
  }

  /** Emit a `nav` message when the top-level url changed since the last report. */
  private async reportNav(): Promise<void> {
    const url = this.page.url();
    if (url === this.lastNav || url === "about:blank") return;
    this.lastNav = url;
    const title = await this.page.title();
    this.lastTitle = title;
    this.emit({ type: "nav", url, title });
    this.logger.info("panel nav", { url: redactUrl(url) });
  }

  /** Fan one message to every live subscriber. A broken subscriber never takes
   *  the panel down. */
  private emit(msg: PanelMessage): void {
    for (const fn of this.subs) {
      try {
        fn(msg);
      } catch (err) {
        this.logger.warn("panel subscriber failed", { detail: (err as Error).message });
      }
    }
  }

  /** Attach a subscriber. Sends `ready` and — if the panel has already committed
   *  a url — the current `nav` (so a client attaching AFTER start() sees the
   *  address bar, since screencast frames are live-only with no replay). Returns
   *  an unsubscribe fn. */
  subscribe(fn: (m: PanelMessage) => void): () => void {
    this.subs.add(fn);
    fn({ type: "ready", panelId: this.panelId, wire: PANEL_WIRE_VERSION, viewport: { ...this.viewport } });
    if (this.lastNav) fn({ type: "nav", url: this.lastNav, title: this.lastTitle });
    return () => {
      this.subs.delete(fn);
    };
  }

  /** Dispatch one client input event into the page. `resize` re-emulates device
   *  metrics and restarts the screencast at the new size; everything else maps
   *  straight to a CDP command. Never throws — a bad event is logged + dropped. */
  async dispatch(ev: InputEvent): Promise<void> {
    if (this.status === "closed") return;
    try {
      if (ev.type === "resize") {
        const width = clampDim(ev.width, this.viewport.width);
        const height = clampDim(ev.height, this.viewport.height);
        if (width === this.viewport.width && height === this.viewport.height) return;
        this.viewport = { width, height };
        const metrics = resizeMetrics(width, height);
        await this.page.send(metrics.method, metrics.params);
        // Restart the screencast so maxWidth/maxHeight track the new viewport.
        await this.page.send("Page.stopScreencast").catch(() => {});
        await this.startScreencast();
        return;
      }
      const cmd = toCdp(ev);
      if (!cmd) return;
      await this.page.send(cmd.method, cmd.params);
    } catch (err) {
      this.logger.warn("panel input dispatch failed", { type: ev?.type, detail: (err as Error).message });
      this.emit({ type: "error", detail: "input dispatch failed" });
    }
  }

  /** Navigate the panel to a new url (POST /panel/browser/:id/nav). */
  async navigate(url: string): Promise<void> {
    await this.page.goto(url);
    await this.reportNav();
  }

  /** Tear down: stop the screencast, unsubscribe CDP handlers, close the context
   *  (kills the page + all storage), tell subscribers. Idempotent. */
  async close(reason = "closed"): Promise<void> {
    if (this.status === "closed") return;
    this.status = "closed";
    for (const un of this.unsubscribers.splice(0)) {
      try {
        un();
      } catch {
        /* ignore */
      }
    }
    try {
      await this.page.send("Page.stopScreencast");
    } catch {
      /* best-effort */
    }
    await this.page.close();
    this.emit({ type: "closed", reason });
    this.subs.clear();
    this.logger.info("panel closed", { reason });
  }
}
