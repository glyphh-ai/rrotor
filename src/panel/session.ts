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
 * The screencast is the FLOOR, not the legacy. It starts with the panel and
 * NEVER stops; if a peer fails, times out, or the encoder dies, that subscriber
 * goes straight back to receiving JPEG frames with no re-negotiation and no
 * gap. When every subscriber is on proven WebRTC the cast drops to a thumbnail
 * "probe" mode — no frame reaches the wire, it survives only as the motion
 * sensor. The decisions are pure functions in panel/signal.ts — this class
 * only supplies the facts and acts on the verdicts.
 *
 * ── the encoder idle gate ────────────────────────────────────────────────────
 * A still page must cost NOTHING, on either transport — that is what makes
 * WebRTC strictly better than JPEG instead of merely better under motion. The
 * change-driven screencast is the motion signal (a frame arriving IS motion);
 * the pure gate in panel/idle.ts turns that signal into encoder commands:
 *
 *   still for idleAfterMs → PAUSE: capture one freeze-frame, move the WebRTC
 *     subscribers back onto the JPEG screencast (frame first, so the canvas
 *     shows the same pixels the video froze on — a static page must never go
 *     black), stop ffmpeg. Idle cost: zero encoder CPU, zero bytes.
 *   motion returns → RESUME: the full-quality screencast is already carrying
 *     the change to the parked subscribers while ffmpeg respawns (a respawn,
 *     not SIGCONT: a resumed x11grab derails the RTP clock, while a fresh
 *     process re-resolves geometry and opens on a clean IDR — the same seam
 *     `resize` already trusts).
 *   media re-proven (MEDIA_FLOWING_PACKETS since the restart) → REMUTE: the
 *     parked subscribers go back on WebRTC and their JPEG is muted again.
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
import { EncoderIdleGate } from "./idle.js";
import { screencastMode } from "./signal.js";
import type { ScreencastMode, SignalMessage } from "./signal.js";
import { MEDIA_FLOWING_PACKETS, PanelPeer, PanelVideoSource } from "./webrtc.js";
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
  /** The screencast's current shape: `full` (wire-quality, fanned out) or
   *  `probe` (motion-sensor thumbnails, never emitted). */
  screencastMode: ScreencastMode;
  /** Whether the native encoder is currently running. */
  encoderRunning: boolean;
  /** Geometry-driven encoder swaps (resize) — the thrash counter. A client
   *  animating its size shows up here instead of in a port-exhaustion page. */
  encoderRestarts: number;
  /** The capture rect the running (or last) encoder grabs, `"WxH"`. */
  captureSize: string | null;
  /** Idle-gate verdict: `active` (encoder should run), `idle` (page still,
   *  encoder paused), or `off` (no gate — screencast-only panel or
   *  PANEL_IDLE_AFTER_MS=0). */
  encoderGate: "active" | "idle" | "off";
  /** Total idle⇄active gate transitions — the flap counter. */
  gateTransitions: number;
  /** Motion→first-RTP-packet latency of the most recent encoder resume (ms). */
  lastResumeMs: number | null;
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
  /** On the screencast only because the idle gate parked it there — its peer is
   *  alive and proven, and it goes back to WebRTC on the next remute. Cleared
   *  the moment the peer itself falls back for any real reason. */
  idleParked = false;
  closed = false;
  constructor(readonly send: (m: PanelMessage) => void) {}
}

/** Probe-mode screencast geometry/quality: big enough that Chromium still emits
 *  a frame per visual change, small enough that encoding it is negligible next
 *  to the video encoder it supervises. Never reaches the wire. */
const PROBE_MAX_DIM = 96;
const PROBE_QUALITY = 20;

/** How long after a cast (re)start its frames are treated as settle repaints
 *  rather than motion. Longer than Chromium's start-of-cast emission burst,
 *  far shorter than any idle window worth configuring. */
const CAST_SETTLE_MS = 250;

/** Trailing settle for ENCODER restarts on resize. A client animating its panel
 *  open fires a resize per frame (observed live: 6+ encoder restarts in 100ms,
 *  each binding a fresh RTP port, at sliver sizes like 500×17); the pod must
 *  defend itself regardless of client behaviour. The page resize and the
 *  screencast re-shape stay immediate (cheap); only the ffmpeg swap — a whole
 *  process spawn — coalesces to ONE restart at the settled geometry. */
const RESIZE_SETTLE_MS = 300;

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
  private castMode: ScreencastMode = "full";
  /** Chromium emits the CURRENT frame (occasionally more than one) whenever a
   *  screencast starts, changed or not. Those are repaints, not motion —
   *  counting them would make the very reshape a pause performs (probe→full)
   *  look like motion and flap the gate awake forever (observed live). Frames
   *  inside a short settle window after every (re)start are therefore ignored
   *  by the gate; they are still emitted to subscribers, which is exactly how
   *  the pause transition gets its freeze-frame. */
  private suppressMotionUntil = 0;
  private screencastBytes = 0;
  private screencastFrames = 0;

  private readonly webrtcConfig: WebrtcConfig | undefined;
  private readonly podCanWebrtc: boolean;
  /** Set in start(): does THIS page have a capturable display. */
  private pageCanWebrtc = false;
  private source: PanelVideoSource | null = null;

  /** The idle gate (decisions) + its one timer, and the serialized encoder-op
   *  queue (effects). EVERY encoder lifecycle effect — pause, resume, resize
   *  restart — goes through the one chain, so no two can ever interleave and
   *  orphan an ffmpeg or its RTP ports. */
  private gate: EncoderIdleGate | null = null;
  private gateTimer: ReturnType<typeof setTimeout> | null = null;
  private encoderOps: Promise<void> = Promise.resolve();
  /** Trailing resize-settle timer for the debounced encoder restart. */
  private resizeSettleTimer: ReturnType<typeof setTimeout> | null = null;

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
        // Every frame — full or probe — IS the motion signal for the idle gate,
        // EXCEPT the settle repaints a freshly (re)started cast emits.
        if (this.now() >= this.suppressMotionUntil) this.noteMotion();
        // Probe frames exist only for that signal: they are never fanned out and
        // never counted as wire bytes, because they never leave the pod.
        if (this.castMode === "full") {
          this.screencastFrames++;
          this.screencastBytes += p.data?.length ?? 0;
          this.emitFrame({
            type: "frame",
            data: p.data,
            format: "jpeg",
            meta: { deviceWidth: p.metadata?.deviceWidth ?? this.viewport.width, deviceHeight: p.metadata?.deviceHeight ?? this.viewport.height },
            at: this.now(),
          });
        }
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
      { onStarted: () => this.onEncoderStarted(), onPacket: () => this.onEncoderPacket() },
    );
    if (this.webrtcConfig.idleAfterMs > 0) {
      this.gate = new EncoderIdleGate({ idleAfterMs: this.webrtcConfig.idleAfterMs, proofPackets: MEDIA_FLOWING_PACKETS });
    }
    this.logger.info("panel webrtc available", {
      size: `${target.width}x${target.height}`,
      codec: this.webrtcConfig.encoder.codec,
      idle_after_ms: this.webrtcConfig.idleAfterMs,
    });
  }

  /** The encoder died. Every WebRTC subscriber is failed back to the screencast;
   *  none of them has to notice, because syncScreencast restarts the cast in the
   *  same tick. */
  private onVideoFailure(reason: string): void {
    this.logger.warn("panel video source failed — falling back", { detail: reason });
    for (const sub of this.subs) {
      sub.idleParked = false;
      if (sub.transport !== "webrtc") continue;
      sub.transport = "screencast";
      this.deliver(sub, { type: "transport", transport: "screencast", reason: `webrtc failed: ${reason}` });
      // The screencast is change-driven, so a STILL page would leave this
      // subscriber staring at a stale canvas until the next repaint. Same cure
      // as attach: one keyframe, immediately.
      void this.sendKeyframe(sub);
    }
    void this.syncScreencast();
  }

  private async startScreencast(mode: ScreencastMode = "full"): Promise<void> {
    if (this.screencastOn) return;
    const probe = mode === "probe";
    await this.page.send("Page.startScreencast", {
      format: "jpeg",
      quality: probe ? PROBE_QUALITY : this.quality,
      // Full mode captures at PHYSICAL resolution (CSS px × devicePixelRatio) so
      // a Retina/phone client is crisp; probe mode is a thumbnail — its frames
      // exist only as the idle gate's motion signal and are never emitted.
      maxWidth: probe ? PROBE_MAX_DIM : Math.round(this.viewport.width * this.dpr),
      maxHeight: probe ? PROBE_MAX_DIM : Math.round(this.viewport.height * this.dpr),
      everyNthFrame: 1,
    });
    this.screencastOn = true;
    this.castMode = mode;
    this.suppressMotionUntil = this.now() + CAST_SETTLE_MS;
  }

  private async stopScreencast(): Promise<void> {
    if (!this.screencastOn) return;
    this.screencastOn = false;
    await this.page.send("Page.stopScreencast").catch(() => {
      /* a closing page refusing to stop its cast is not worth surfacing */
    });
  }

  /** Re-shape the CDP screencast to match what the subscribers need (full vs
   *  probe — it never stops while the panel lives; it is the motion sensor).
   *  This is the ONLY place the cast is reconfigured after start(), so the
   *  "JPEG must remain the fallback" rule cannot be violated by accident. */
  private async syncScreencast(): Promise<void> {
    // Before the panel is live, start() owns the screencast — syncing here would
    // race it and double-start the cast.
    if (this.status !== "live") return;
    const mode = screencastMode([...this.subs].map((s) => s.transport));
    if (this.screencastOn && mode === this.castMode) return;
    try {
      await this.stopScreencast();
      await this.startScreencast(mode);
    } catch (err) {
      this.logger.warn("panel screencast sync failed", { detail: (err as Error).message });
    }
  }

  // ── the encoder idle gate (decisions in panel/idle.ts; effects here) ────────

  /** A screencast frame arrived — feed the gate its motion signal. */
  private noteMotion(): void {
    if (!this.gate) return;
    if (this.gate.motion(this.now()) === "resume") {
      this.logger.info("panel encoder resuming — motion after idle", { transitions: this.gate.transitions });
      this.queueEncoderOp(() => this.gateResume());
    }
    this.armGateTimer();
  }

  /** PanelVideoSource tap: the encoder (re)started. */
  private onEncoderStarted(): void {
    this.gate?.started(this.now());
  }

  /** PanelVideoSource tap: one RTP packet out. Proof reached → remute. */
  private onEncoderPacket(): void {
    if (!this.gate) return;
    if (this.gate.packet(this.now()) === "remute") {
      this.gateRemute();
      this.armGateTimer();
    }
  }

  /** (Re)arm the single stillness timer from the gate's own deadline. */
  private armGateTimer(): void {
    if (this.gateTimer) {
      clearTimeout(this.gateTimer);
      this.gateTimer = null;
    }
    if (!this.gate || this.status === "closed") return;
    const at = this.gate.nextCheckAt();
    if (at === null) return;
    this.gateTimer = setTimeout(() => {
      this.gateTimer = null;
      this.runGateCheck();
    }, Math.max(16, at - this.now()));
    if (typeof this.gateTimer.unref === "function") this.gateTimer.unref();
  }

  private runGateCheck(): void {
    if (!this.gate || this.status !== "live") return;
    if (this.gate.check(this.now()) === "pause") this.queueEncoderOp(() => this.gatePause());
    else this.armGateTimer();
  }

  /** Serialize encoder lifecycle effects: pause, resume and resize restarts
   *  must never interleave — that is what guarantees no ffmpeg (and no RTP
   *  port pair) is ever orphaned by overlapping transitions. */
  private queueEncoderOp(op: () => Promise<void>): void {
    this.encoderOps = this.encoderOps.then(op).catch((err: unknown) => {
      this.logger.warn("panel encoder transition failed", { detail: (err as Error).message });
    });
  }

  /**
   * A resize arrived: (re)arm the trailing settle and swap the encoder ONCE at
   * the final geometry. An idle-gated panel schedules nothing — its encoder is
   * paused, and resume re-measures the capture rect anyway. The restart itself
   * re-resolves the rect at fire time, so a burst always lands on the size the
   * client settled at, and a sliver rect is skipped inside the source.
   */
  private scheduleEncoderRestart(): void {
    if (!this.source) return;
    if (this.resizeSettleTimer) clearTimeout(this.resizeSettleTimer);
    this.resizeSettleTimer = setTimeout(() => {
      this.resizeSettleTimer = null;
      if (this.status !== "live" || !this.source) return;
      if (this.gate?.state === "idle") return;
      this.queueEncoderOp(() => this.source?.restart() ?? Promise.resolve());
    }, RESIZE_SETTLE_MS);
    if (typeof this.resizeSettleTimer.unref === "function") this.resizeSettleTimer.unref();
  }

  /**
   * The page has been still for the whole idle window: park every WebRTC
   * subscriber back on the screencast and stop the encoder.
   *
   * The freeze-frame comes from the cast reshape itself: switching probe→full
   * makes Chromium emit the current frame unprompted, and it is fanned to the
   * just-parked subscribers like any full-mode frame — so the canvas each
   * client switches to shows the exact pixels its video froze on, and a still
   * page never goes black or stale across the transition. NOT a
   * `Page.captureScreenshot`: a screenshot forces a compositor commit, the
   * still-running cast captures that commit, and the gate reads its own
   * freeze-frame as motion — the flap loop the live test caught.
   */
  private async gatePause(): Promise<void> {
    if (this.status !== "live" || !this.source) return;
    let parked = 0;
    for (const sub of this.subs) {
      if (sub.closed || sub.transport !== "webrtc") continue;
      sub.transport = "screencast";
      sub.idleParked = true;
      this.deliver(sub, { type: "transport", transport: "screencast", reason: "encoder idle — page still" });
      parked++;
    }
    await this.source.pause();
    await this.syncScreencast();
    this.logger.info("panel encoder paused — page still", { parked, transitions: this.gate?.transitions ?? 0 });
  }

  /** Motion while paused: respawn the encoder. The full-quality screencast is
   *  already carrying the motion to the parked subscribers, so nothing waits. */
  private async gateResume(): Promise<void> {
    if (this.status !== "live" || !this.source) return;
    try {
      await this.source.resume();
    } catch (err) {
      this.logger.warn("panel encoder resume failed — parked subscribers stay on screencast", { detail: (err as Error).message });
    }
  }

  /** The restarted encoder re-proved itself: promote the idle-parked
   *  subscribers back onto WebRTC and mute their JPEG again. */
  private gateRemute(): void {
    let promoted = 0;
    for (const sub of this.subs) {
      if (!sub.idleParked || sub.closed || !sub.peer) continue;
      sub.idleParked = false;
      sub.transport = "webrtc";
      this.deliver(sub, { type: "transport", transport: "webrtc", reason: "encoder resumed — media flowing" });
      promoted++;
    }
    if (promoted > 0) {
      this.logger.info("panel encoder proven — webrtc re-muted jpeg", { subscribers: promoted, resume_ms: this.gate?.lastResumeMs ?? null });
    }
    void this.syncScreencast();
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
        const was = sub.transport;
        sub.transport = decision.transport;
        if (decision.transport !== "webrtc") {
          // A real fallback (timeout, failure, peer death) outranks the idle
          // gate's parking — this subscriber is no longer remute material.
          sub.idleParked = false;
          // Falling off webrtc onto a change-driven cast on a STILL page would
          // freeze the client on stale pixels; paint the present, once.
          if (was === "webrtc") void this.sendKeyframe(sub);
        }
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
        // Restart the screencast so maxWidth/maxHeight track the new viewport
        // (in whatever mode it was already in)…
        if (this.screencastOn) {
          const mode = this.castMode;
          this.screencastOn = false;
          await this.page.send("Page.stopScreencast").catch(() => {});
          await this.startScreencast(mode);
        }
        // …and schedule ONE encoder swap for when the geometry settles — a
        // resize-per-frame animation must not burn an ffmpeg spawn per frame
        // (and an idle-paused encoder stays paused; resume re-measures anyway).
        this.scheduleEncoderRestart();
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
      screencastMode: this.castMode,
      encoderRunning: this.source?.running === true,
      encoderRestarts: this.source?.restarts ?? 0,
      captureSize: this.source?.lastTarget ? `${this.source.lastTarget.width}x${this.source.lastTarget.height}` : null,
      encoderGate: this.gate ? this.gate.state : "off",
      gateTransitions: this.gate?.transitions ?? 0,
      lastResumeMs: this.gate?.lastResumeMs ?? null,
      uptimeMs: Date.now() - this.startedAt,
    };
  }

  /** Tear down: stop the video, unsubscribe CDP handlers, close the context
   *  (kills the page + all storage), tell subscribers. Idempotent. */
  async close(reason = "closed"): Promise<void> {
    if (this.status === "closed") return;
    const finalStats = this.stats();
    this.status = "closed";
    if (this.gateTimer) {
      clearTimeout(this.gateTimer);
      this.gateTimer = null;
    }
    if (this.resizeSettleTimer) {
      clearTimeout(this.resizeSettleTimer);
      this.resizeSettleTimer = null;
    }
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
