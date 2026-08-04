/**
 * panel/session.ts — ONE browser panel: a real page in the pod, its video fanned
 * out to attached clients over whichever transport each client could get, and
 * client input dispatched back into it.
 *
 * A `PanelSession` owns a {@link PanelPage} (an isolated browser context) and:
 *   • starts a CDP screencast → each `Page.screencastFrame` becomes a `frame`
 *     wire message, ACKed per protocol (mandatory — Chromium stalls the
 *     screencast until the frame is ACKed), and fanned out to every subscriber
 *     that is still on the screencast transport.
 *   • negotiates WEBRTC per subscriber when the client asks for it and the pod
 *     can serve it (a capturable X display + ffmpeg + werift), muting that
 *     subscriber's JPEG frames only once its peer is actually carrying media.
 *   • subscribes `Page.frameNavigated` → emits a `nav` message on url/title
 *     change so the client's address bar tracks the real page.
 *   • takes {@link InputEvent}s off the WS, maps them via panel/input.ts, and
 *     dispatches the CDP command; a `resize` re-sizes the page AND restarts
 *     whichever video path is live at the new size.
 *   • tears the context down on `close()` — no orphan Chromium page/context,
 *     no orphan ffmpeg, no orphan peer connection.
 *
 * ── two transports, one fallback rule ───────────────────────────────────────
 * The screencast is the FLOOR, not the legacy. It starts with the panel and runs
 * until every attached subscriber has proven a working peer connection; if a
 * peer fails, times out, or the encoder dies, that subscriber goes straight back
 * to receiving JPEG frames with no re-negotiation and no gap. The decision
 * itself is a pure function in panel/signal.ts — this class only supplies the
 * facts and acts on the verdict.
 *
 * INPUT IS UNCHANGED by any of this. Mouse, keys, wheel and resize map to CDP
 * through panel/input.ts exactly as before, on both transports; only the pixels
 * take a different road.
 *
 * Screencast is LIVE-ONLY (no replay ring, unlike the harness): a late/re-
 * attaching subscriber receives a keyframe on attach and then the next change.
 * frames.ts documents why.
 *
 * The session is transport-agnostic at its edge: it fans `PanelMessage`s to
 * subscriber callbacks. The WS server (server.ts) is one subscriber; a test is
 * another.
 */

import { log } from "../obs/logger.js";
import type { PanelPage } from "./browser.js";
import { PANEL_WIRE_VERSION, redactUrl } from "./frames.js";
import type { PanelCapabilities, PanelMessage, PanelTransport } from "./frames.js";
import { toCdp, resizeMetrics, clampDim } from "./input.js";
import type { InputEvent } from "./input.js";
import { screencastNeeded } from "./signal.js";
import type { SignalMessage } from "./signal.js";
import { PanelPeer, PanelVideoSource } from "./webrtc.js";
import type { WebrtcConfig } from "./webrtc.js";

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
  /** Screencast image quality 0–100 (jpeg). Default 80. */
  quality?: number;
  /** Client devicePixelRatio (1–3) — frames captured at physical resolution. */
  deviceScaleFactor?: number;
  /** Injectable clock for deterministic `at` stamps in tests. */
  now?: () => number;
  /** WebRTC settings. Absent → this panel is screencast-only. */
  webrtc?: WebrtcConfig;
  /** Whether the POD can do WebRTC at all (werift + ffmpeg present). The PAGE's
   *  half of the capability — does it have a capturable display — is probed in
   *  {@link PanelSession.start}. */
  webrtcAvailable?: boolean;
}

/**
 * What the panel costs on the wire. Both transports are counted the same way —
 * payload bytes leaving the pod — so the two are directly comparable, which is
 * the entire point of measuring them, and this is the seam per-minute bandwidth
 * metering will eventually read.
 */
export interface PanelStats {
  panelId: string;
  /** Base64 JPEG payload bytes fanned out over the WS. */
  screencastBytes: number;
  screencastFrames: number;
  /** RTP payload bytes produced by the encoder (shared across this panel's peers). */
  videoBytes: number;
  /** Per-subscriber transports, in attach order. */
  transports: PanelTransport[];
  /** Whether the CDP screencast is currently running. */
  screencastRunning: boolean;
  /** Whether the native encoder is currently running. */
  encoderRunning: boolean;
  uptimeMs: number;
}

/** The handle the WS server holds for one attached client. */
export interface PanelSubscriber {
  /** Feed one validated client→server signaling message to this subscriber. */
  signal(msg: SignalMessage): Promise<void>;
  /** The transport this subscriber's pixels are arriving on right now. */
  readonly transport: PanelTransport;
  /** Detach (and tear down this subscriber's peer). Idempotent. */
  close(): Promise<void>;
}

/** Internal per-subscriber state. */
class Subscriber {
  transport: PanelTransport = "screencast";
  peer: PanelPeer | null = null;
  closed = false;
  constructor(readonly send: (m: PanelMessage) => void) {}
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
  private readonly dpr: number;
  private readonly now: () => number;
  private readonly logger;
  private readonly startedAt = Date.now();

  private readonly subs = new Set<Subscriber>();
  private readonly unsubscribers: Array<() => void> = [];
  private lastNav = "";
  private lastTitle = "";

  private screencastOn = false;
  private screencastBytes = 0;
  private screencastFrames = 0;

  private readonly webrtcConfig: WebrtcConfig | undefined;
  private readonly podCanWebrtc: boolean;
  /** Set in start(): does THIS page have a capturable display. */
  private pageCanWebrtc = false;
  private source: PanelVideoSource | null = null;

  constructor(opts: PanelSessionOptions & { sessionId?: string }) {
    this.panelId = opts.panelId;
    this.sessionId = opts.sessionId ?? "";
    this.page = opts.page;
    this.viewport = { width: clampDim(opts.viewport.width, 1024), height: clampDim(opts.viewport.height, 720) };
    this.quality = Math.min(100, Math.max(1, opts.quality ?? 80));
    this.dpr = Math.min(3, Math.max(1, opts.deviceScaleFactor ?? 1));
    this.now = opts.now ?? Date.now;
    this.webrtcConfig = opts.webrtc;
    this.podCanWebrtc = opts.webrtcAvailable === true && opts.webrtc !== undefined;
    this.logger = log.child({ panel_id: this.panelId });
  }

  /** What this panel can offer a client, announced in `ready`. */
  capabilities(): PanelCapabilities {
    const webrtc = this.podCanWebrtc && this.pageCanWebrtc;
    return { webrtc, codecs: webrtc && this.webrtcConfig ? [this.webrtcConfig.encoder.codec] : [] };
  }

  /** Wire CDP events and start the screencast. Call once, after the page has
   *  navigated to its initial url. */
  async start(): Promise<void> {
    // Screencast frames → fan out + ACK (ACK is mandatory or Chromium stalls).
    this.unsubscribers.push(
      this.page.on("Page.screencastFrame", (p: ScreencastFramePayload) => {
        this.screencastFrames++;
        this.screencastBytes += p.data?.length ?? 0;
        this.emitFrame({
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
    await this.probeWebrtcCapability();
    this.status = "live";
    await this.reportNav();
  }

  /** Ask the page whether it has a capturable display, and if so stand up this
   *  panel's (lazy, refcounted) video source. A page without one is simply a
   *  screencast-only panel — never an error. */
  private async probeWebrtcCapability(): Promise<void> {
    if (!this.podCanWebrtc || !this.webrtcConfig || !this.page.capture) return;
    let target = null;
    try {
      target = await this.page.capture();
    } catch (err) {
      this.logger.warn("panel capture probe failed", { detail: (err as Error).message });
    }
    if (!target) {
      this.logger.info("panel has no capturable display — screencast only", {});
      return;
    }
    this.pageCanWebrtc = true;
    this.source = new PanelVideoSource(
      this.panelId,
      this.webrtcConfig,
      () => (this.page.capture ? this.page.capture() : Promise.resolve(null)),
      (reason) => this.onVideoFailure(reason),
    );
    this.logger.info("panel webrtc available", { size: `${target.width}x${target.height}`, codec: this.webrtcConfig.encoder.codec });
  }

  /** The encoder died. Every WebRTC subscriber is failed back to the screencast;
   *  none of them has to notice, because syncScreencast restarts the cast in the
   *  same tick. */
  private onVideoFailure(reason: string): void {
    this.logger.warn("panel video source failed — falling back", { detail: reason });
    for (const sub of this.subs) {
      if (sub.transport !== "webrtc") continue;
      sub.transport = "screencast";
      this.deliver(sub, { type: "transport", transport: "screencast", reason: `webrtc failed: ${reason}` });
    }
    void this.syncScreencast();
  }

  private async startScreencast(): Promise<void> {
    if (this.screencastOn) return;
    await this.page.send("Page.startScreencast", {
      format: "jpeg",
      quality: this.quality,
      // Capture at PHYSICAL resolution (CSS px × devicePixelRatio) so a Retina/
      // phone client is crisp; a DPR-1 desktop is unchanged.
      maxWidth: Math.round(this.viewport.width * this.dpr),
      maxHeight: Math.round(this.viewport.height * this.dpr),
      everyNthFrame: 1,
    });
    this.screencastOn = true;
  }

  private async stopScreencast(): Promise<void> {
    if (!this.screencastOn) return;
    this.screencastOn = false;
    await this.page.send("Page.stopScreencast").catch(() => {
      /* a closing page refusing to stop its cast is not worth surfacing */
    });
  }

  /** Start or stop the CDP screencast to match what the subscribers need. This
   *  is the ONLY place the screencast is turned on/off after start(), so the
   *  "JPEG must remain the fallback" rule cannot be violated by accident. */
  private async syncScreencast(): Promise<void> {
    // Before the panel is live, start() owns the screencast — syncing here would
    // race it and double-start the cast.
    if (this.status !== "live") return;
    const needed = screencastNeeded([...this.subs].map((s) => s.transport));
    try {
      if (needed) await this.startScreencast();
      else await this.stopScreencast();
    } catch (err) {
      this.logger.warn("panel screencast sync failed", { detail: (err as Error).message });
    }
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
    for (const sub of this.subs) this.deliver(sub, msg);
  }

  /** Fan a screencast frame to the subscribers still ON the screencast. A
   *  subscriber whose WebRTC is carrying media must not also receive JPEG — that
   *  would double its bandwidth and defeat the entire exercise. */
  private emitFrame(msg: PanelMessage): void {
    for (const sub of this.subs) {
      if (sub.transport === "webrtc") continue;
      this.deliver(sub, msg);
    }
  }

  private deliver(sub: Subscriber, msg: PanelMessage): void {
    try {
      sub.send(msg);
    } catch (err) {
      this.logger.warn("panel subscriber failed", { detail: (err as Error).message });
    }
  }

  /** How many clients are attached right now (0 → the panel is ORPHANED and the
   *  registry's grace-period reaper will reclaim it). */
  get subscriberCount(): number {
    return this.subs.size;
  }

  /**
   * Attach a subscriber and get back the handle the WS server drives.
   *
   * Sends `ready` (with this panel's capabilities), the current `nav` if the
   * panel has committed a url, then a keyframe. WebRTC is NOT started here: a
   * client opts in by sending `hello{webrtc:true}`, which is exactly what keeps
   * a v1 client's behaviour bit-for-bit unchanged.
   */
  attach(send: (m: PanelMessage) => void): PanelSubscriber {
    const sub = new Subscriber(send);
    this.subs.add(sub);
    this.deliver(sub, {
      type: "ready",
      panelId: this.panelId,
      wire: PANEL_WIRE_VERSION,
      viewport: { ...this.viewport },
      capabilities: this.capabilities(),
    });
    if (this.lastNav) this.deliver(sub, { type: "nav", url: this.lastNav, title: this.lastTitle });
    // KEYFRAME ON ATTACH: the screencast is change-driven, so a subscriber that
    // attaches after a static page finished painting would see BLACK until the next
    // visual change. Capture the current screen once and send it immediately so the
    // panel shows the live page the instant it opens.
    void this.sendKeyframe(sub);
    void this.syncScreencast();
    return {
      signal: (msg) => this.handleSignal(sub, msg),
      get transport(): PanelTransport {
        return sub.transport;
      },
      close: () => this.detach(sub),
    };
  }

  /** v1-compatible attach: a plain unsubscribe fn, no signaling. Used by tests
   *  and by any caller that only wants the screencast. */
  subscribe(fn: (m: PanelMessage) => void): () => void {
    const handle = this.attach(fn);
    return () => {
      void handle.close();
    };
  }

  private async detach(sub: Subscriber): Promise<void> {
    if (sub.closed) return;
    sub.closed = true;
    this.subs.delete(sub);
    const peer = sub.peer;
    sub.peer = null;
    if (peer) await peer.close();
    await this.syncScreencast();
  }

  /** Route one validated signaling message for one subscriber. */
  private async handleSignal(sub: Subscriber, msg: SignalMessage): Promise<void> {
    if (sub.closed || this.status === "closed") return;
    if (msg.type === "hello") {
      await this.startNegotiation(sub, msg.webrtc);
      return;
    }
    if (!sub.peer) return;
    if (msg.type === "answer") await sub.peer.acceptAnswer(msg.sdp);
    else if (msg.type === "ice") await sub.peer.acceptCandidate(msg.candidate);
  }

  /** Begin WebRTC for one subscriber, or tell it plainly why it is not getting
   *  any. Either way the subscriber ends up with a `transport` message, so the
   *  client never has to guess which path its pixels are on. */
  private async startNegotiation(sub: Subscriber, wants: boolean): Promise<void> {
    if (sub.peer) return;
    const caps = this.capabilities();
    if (!wants || !caps.webrtc || !this.source || !this.webrtcConfig) {
      const reason = !wants ? "client did not request webrtc" : "pod has no capturable display or encoder";
      this.deliver(sub, { type: "transport", transport: "screencast", reason });
      return;
    }
    const peer = new PanelPeer({
      panelId: this.panelId,
      source: this.source,
      config: this.webrtcConfig,
      send: (m) => this.deliver(sub, m),
      onTransport: (decision) => {
        sub.transport = decision.transport;
        void this.syncScreencast();
      },
    });
    sub.peer = peer;
    await peer.start();
  }

  /** Capture the current page as one jpeg frame and send it to a single subscriber —
   *  the initial paint for a client that just attached. Best-effort; never throws. */
  private async sendKeyframe(sub: Subscriber): Promise<void> {
    if (this.status === "closed" || sub.closed) return;
    try {
      const shot = await this.page.send("Page.captureScreenshot", {
        format: "jpeg", quality: this.quality, captureBeyondViewport: false,
      }) as { data?: string };
      // By the time the screenshot lands the subscriber may already be on WebRTC,
      // in which case its video is the truth and a stale JPEG would only flicker.
      if (!shot?.data || sub.closed || sub.transport === "webrtc") return;
      this.screencastBytes += shot.data.length;
      this.screencastFrames++;
      this.deliver(sub, {
        type: "frame",
        data: shot.data,
        format: "jpeg",
        meta: { deviceWidth: Math.round(this.viewport.width * this.dpr), deviceHeight: Math.round(this.viewport.height * this.dpr) },
        at: this.now(),
      });
    } catch {
      /* a page mid-navigation can refuse a screenshot — the screencast covers the next paint */
    }
  }

  /** Dispatch one client input event into the page. `resize` re-sizes the page and
   *  restarts whichever video path is live; everything else maps straight to a CDP
   *  command. Never throws — a bad event is logged + dropped. */
  async dispatch(ev: InputEvent): Promise<void> {
    if (this.status === "closed") return;
    try {
      if (ev.type === "resize") {
        const width = clampDim(ev.width, this.viewport.width);
        const height = clampDim(ev.height, this.viewport.height);
        if (width === this.viewport.width && height === this.viewport.height) return;
        this.viewport = { width, height };
        await this.resizePage(width, height);
        // Restart the screencast so maxWidth/maxHeight track the new viewport…
        if (this.screencastOn) {
          this.screencastOn = false;
          await this.page.send("Page.stopScreencast").catch(() => {});
          await this.startScreencast();
        }
        // …and the encoder, whose x11grab rect is fixed for the life of the
        // process and now points at the wrong geometry.
        if (this.source) await this.source.restart();
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

  /**
   * Make the page render at a new size.
   *
   * A HEADFUL page must resize its real OS WINDOW — emulating device metrics
   * would decouple the rendered size from the window the encoder is grabbing,
   * and the stream would silently show the wrong rect. A headless page has no
   * window, so it uses metric emulation exactly as v1 did.
   */
  private async resizePage(width: number, height: number): Promise<void> {
    if (this.page.resize) {
      await this.page.resize(width, height);
      return;
    }
    const metrics = resizeMetrics(width, height, this.dpr);
    await this.page.send(metrics.method, metrics.params);
  }

  /** Navigate the panel to a new url (POST /panel/browser/:id/nav). */
  async navigate(url: string): Promise<void> {
    await this.page.goto(url);
    await this.reportNav();
  }

  /** Byte/transport accounting for this panel. */
  stats(): PanelStats {
    return {
      panelId: this.panelId,
      screencastBytes: this.screencastBytes,
      screencastFrames: this.screencastFrames,
      videoBytes: this.source?.bytesOut ?? 0,
      transports: [...this.subs].map((s) => s.transport),
      screencastRunning: this.screencastOn,
      encoderRunning: this.source?.running === true,
      uptimeMs: Date.now() - this.startedAt,
    };
  }

  /** Tear down: stop the video, unsubscribe CDP handlers, close the context
   *  (kills the page + all storage), tell subscribers. Idempotent. */
  async close(reason = "closed"): Promise<void> {
    if (this.status === "closed") return;
    const finalStats = this.stats();
    this.status = "closed";
    for (const un of this.unsubscribers.splice(0)) {
      try {
        un();
      } catch {
        /* ignore */
      }
    }
    for (const sub of this.subs) {
      const peer = sub.peer;
      sub.peer = null;
      if (peer) await peer.close().catch(() => {});
    }
    if (this.source) await this.source.stop().catch(() => {});
    this.source = null;
    try {
      await this.page.send("Page.stopScreencast");
    } catch {
      /* best-effort */
    }
    this.screencastOn = false;
    await this.page.close();
    this.emit({ type: "closed", reason });
    this.subs.clear();
    this.logger.info("panel closed", { reason, screencast_bytes: finalStats.screencastBytes, video_bytes: finalStats.videoBytes });
  }
}
