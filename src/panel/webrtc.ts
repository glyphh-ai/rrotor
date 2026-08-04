/**
 * panel/webrtc.ts — the v2 video transport: ONE encoder per panel, ONE peer
 * connection per subscriber, and the fallback machinery that makes both
 * optional.
 *
 * The division of labour is the point of the design:
 *
 *   ffmpeg (native)   captures the X display and produces already-payloadized
 *                     H.264/VP8 RTP  — see panel/encoder.ts
 *   werift (pure TS)  does ICE, DTLS and SRTP and forwards those RTP packets
 *                     — no native peer library to build into the image
 *   this file         refcounts the encoder, mirrors packets to peers, drives
 *                     signaling, and decides when a subscriber is really on
 *                     WebRTC
 *
 * Node never touches a pixel: a packet arrives on a loopback socket, is handed
 * to `track.writeRtp`, and leaves encrypted. That is the entire per-frame cost
 * in JS, and it is why this path can be ~an order of magnitude cheaper on the
 * wire than base64 JPEG over a WebSocket without being more expensive in CPU.
 *
 * ── the encoder is refcounted, and that is load-bearing ─────────────────────
 * The encoder starts when the FIRST peer actually connects and stops when the
 * LAST one goes away. An idle panel therefore burns no encoder CPU at all, and
 * because a connect is always immediately preceded by an encoder start, the
 * joining peer gets a keyframe for free — which is what lets the keyframe
 * interval stay long (a short one would tax every idle panel forever to save a
 * late joiner one wait; see encoder.ts).
 *
 * ── nothing here is allowed to be required ──────────────────────────────────
 * werift is imported DYNAMICALLY. If the module is absent, if ffmpeg is absent,
 * if there is no X display, if ICE never completes, if the encoder dies — every
 * one of those paths ends in the same place: `decideTransport` returns
 * `screencast`, the client is told, and the JPEG path it never stopped receiving
 * carries on. WebRTC is an optimisation layered on a transport that already
 * works; it is never a dependency.
 *
 * Secret hygiene: SDP and ICE candidate lines carry DTLS fingerprints and host
 * IP addresses and are NEVER logged. Only candidate type/protocol counts
 * ({@link describeCandidate}) and connection states are.
 */

import { log } from "../obs/logger.js";
import type { PanelIceCandidate, PanelMessage } from "./frames.js";
import { describeCandidate, decideTransport, announceCandidate, announceSdp } from "./signal.js";
import type { TransportDecision } from "./signal.js";
import { X11Encoder, DEFAULT_ENCODER, PAYLOAD_TYPES, hasFfmpeg } from "./encoder.js";
import type { CaptureTarget, EncoderConfig, VideoCodec } from "./encoder.js";

/** Minimal structural view of what we use from werift, so this module type-checks
 *  and unit-tests without the real library being loaded. */
interface WeriftTrack {
  writeRtp(packet: Buffer): void;
  stop(): void;
}
interface WeriftPc {
  connectionState: string;
  localDescription: { sdp: string } | null;
  connectionStateChange: { subscribe(fn: (state: string) => void): unknown };
  onIceCandidate: { subscribe(fn: (candidate: { candidate: string; sdpMid?: string; sdpMLineIndex?: number } | undefined) => void): unknown };
  addTransceiver(track: WeriftTrack, opts: { direction: string }): unknown;
  createOffer(): Promise<{ sdp: string }>;
  setLocalDescription(desc?: unknown): Promise<unknown>;
  setRemoteDescription(desc: { type: string; sdp: string }): Promise<void>;
  addIceCandidate(candidate: unknown): Promise<void>;
  close(): Promise<void>;
}

/** How the pod's peer connections reach the network. Every field here exists
 *  because of a real deployment constraint — see `iceHostAddresses`. */
export interface WebrtcNetworkConfig {
  /**
   * Extra host addresses to advertise as ICE candidates.
   *
   * A pod behind NAT (a container with a published port, a Fly machine behind
   * the anycast proxy) gathers candidates for its PRIVATE address, which the
   * client cannot reach. There is no STUN that fixes this for a server: the pod
   * has to be TOLD the address the world sees it at. `PANEL_ICE_HOST_IPS`.
   */
  iceHostAddresses: string[];
  /**
   * The address to ADVERTISE for host candidates, when it differs from the one
   * the pod binds. This is the NAT knob (mediasoup's `announcedIp`): inside a
   * container the pod binds `172.17.x.x` but the client must be told the
   * published address. Without it, WebRTC cannot work on any NAT'd host.
   * `PANEL_ICE_ANNOUNCE_IP`.
   */
  announceIp: string;
  /** Bind ICE to a fixed port range so a firewall/port-map can be opened for it
   *  (`PANEL_ICE_PORT_RANGE=41500-41600`). */
  icePortRange?: [number, number] | undefined;
  /** Offer ICE-TCP candidates as well as UDP — the fallback for networks (and
   *  clouds) where UDP does not survive. `PANEL_ICE_TCP=1`. */
  iceUseTcp: boolean;
  /** STUN/TURN servers (`PANEL_ICE_SERVERS=stun:a:3478,turn:u:p@b:3478`). */
  iceServers: Array<{ urls: string; username?: string; credential?: string }>;
}

export interface WebrtcConfig {
  encoder: EncoderConfig;
  network: WebrtcNetworkConfig;
  /** How long a subscriber waits for media before it is failed over to the
   *  screencast. A few seconds: long enough for ICE+DTLS on a sane network,
   *  short enough that a UDP-blocked client is not staring at a stalled panel. */
  negotiationTimeoutMs: number;
  ffmpegPath?: string | undefined;
}

/** Build the WebRTC config from env, with defaults that work on a laptop. */
export function webrtcConfigFromEnv(env: NodeJS.ProcessEnv): WebrtcConfig {
  const codec: VideoCodec = env.PANEL_WEBRTC_CODEC === "vp8" ? "vp8" : "h264";
  const num = (raw: string | undefined, fallback: number): number => {
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? n : fallback;
  };
  return {
    encoder: {
      codec,
      payloadType: PAYLOAD_TYPES[codec],
      fps: Math.min(60, num(env.PANEL_WEBRTC_FPS, DEFAULT_ENCODER.fps)),
      bitrateKbps: num(env.PANEL_WEBRTC_BITRATE_KBPS, DEFAULT_ENCODER.bitrateKbps),
      crf: Math.min(51, num(env.PANEL_WEBRTC_CRF, DEFAULT_ENCODER.crf)),
      keyint: num(env.PANEL_WEBRTC_KEYINT, DEFAULT_ENCODER.keyint),
      pktSize: DEFAULT_ENCODER.pktSize,
    },
    network: {
      iceHostAddresses: (env.PANEL_ICE_HOST_IPS ?? "").split(",").map((s) => s.trim()).filter(Boolean),
      announceIp: (env.PANEL_ICE_ANNOUNCE_IP ?? "").trim(),
      icePortRange: parsePortRange(env.PANEL_ICE_PORT_RANGE),
      iceUseTcp: env.PANEL_ICE_TCP === "1",
      iceServers: parseIceServers(env.PANEL_ICE_SERVERS),
    },
    negotiationTimeoutMs: num(env.PANEL_WEBRTC_TIMEOUT_MS, 6000),
    ffmpegPath: env.PANEL_FFMPEG_PATH,
  };
}

/** `"41500-41600"` → `[41500, 41600]`; anything else → undefined (ephemeral). */
export function parsePortRange(raw: string | undefined): [number, number] | undefined {
  const m = /^(\d{3,5})-(\d{3,5})$/.exec((raw ?? "").trim());
  if (!m) return undefined;
  const lo = Number(m[1]);
  const hi = Number(m[2]);
  // werift requires a real range, and ICE needs room for both components.
  if (hi <= lo + 1 || lo < 1024 || hi > 65535) return undefined;
  return [lo, hi];
}

/** `"stun:a:3478,turn:user:pass@b:3478"` → werift's iceServers shape. */
export function parseIceServers(raw: string | undefined): Array<{ urls: string; username?: string; credential?: string }> {
  return (raw ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((entry) => {
      const m = /^(turns?):([^:@]+):([^@]+)@(.+)$/.exec(entry);
      if (m) return { urls: `${m[1]}:${m[4]}`, username: m[2], credential: m[3] };
      return { urls: entry };
    });
}

// ── the per-panel video source ───────────────────────────────────────────────

/**
 * ONE ffmpeg encoder per panel, refcounted across that panel's peers.
 *
 * Every peer gets the same RTP stream; werift re-stamps SSRC per sender, so a
 * second viewer costs bandwidth but not a second encode. That is the property
 * that would make a shared/observed panel affordable later.
 */
export class PanelVideoSource {
  private encoder: X11Encoder | null = null;
  private starting: Promise<void> | null = null;
  private readonly consumers = new Set<(packet: Buffer) => void>();
  private readonly logger;

  constructor(
    private readonly panelId: string,
    private readonly config: WebrtcConfig,
    private readonly resolveTarget: () => Promise<CaptureTarget | null>,
    private readonly onFailure: (reason: string) => void,
  ) {
    this.logger = log.child({ panel_id: panelId });
  }

  /** Bytes the encoder has produced so far — the panel's real video cost. */
  get bytesOut(): number {
    return this.encoder?.bytesOut ?? 0;
  }

  get running(): boolean {
    return this.encoder !== null;
  }

  /** Attach a consumer, starting the encoder if this is the first one. Returns a
   *  detach fn that stops the encoder when the last consumer leaves. */
  async attach(consumer: (packet: Buffer) => void): Promise<() => void> {
    this.consumers.add(consumer);
    try {
      await this.ensureEncoder();
    } catch (err) {
      this.consumers.delete(consumer);
      throw err;
    }
    return () => {
      this.consumers.delete(consumer);
      if (this.consumers.size === 0) void this.stop();
    };
  }

  /** Restart the encoder against a freshly measured capture rect — what a client
   *  `resize` needs, since the window moved and ffmpeg's grab geometry is fixed
   *  for the life of the process. No-op when nothing is consuming. */
  async restart(): Promise<void> {
    if (this.consumers.size === 0) return;
    await this.stopEncoder();
    await this.ensureEncoder().catch((err: unknown) => {
      this.onFailure(`encoder restart failed: ${(err as Error).message}`);
    });
  }

  private async ensureEncoder(): Promise<void> {
    if (this.encoder) return;
    if (this.starting) return this.starting;
    this.starting = (async () => {
      const target = await this.resolveTarget();
      if (!target) throw new Error("no capturable display for this panel");
      const encoder = new X11Encoder({
        config: this.config.encoder,
        target,
        ...(this.config.ffmpegPath ? { ffmpegPath: this.config.ffmpegPath } : {}),
        logFields: { panel_id: this.panelId },
        onRtp: (packet) => {
          for (const consumer of this.consumers) {
            try {
              consumer(packet);
            } catch (err) {
              this.logger.warn("panel rtp consumer failed", { detail: (err as Error).message });
            }
          }
        },
        onExit: (reason) => {
          this.encoder = null;
          this.onFailure(reason);
        },
      });
      await encoder.start();
      this.encoder = encoder;
    })().finally(() => {
      this.starting = null;
    });
    return this.starting;
  }

  private async stopEncoder(): Promise<void> {
    const encoder = this.encoder;
    this.encoder = null;
    if (encoder) await encoder.stop();
  }

  /** Stop the encoder and drop every consumer (panel teardown). */
  async stop(): Promise<void> {
    this.consumers.clear();
    await this.stopEncoder();
  }
}

// ── one peer per subscriber ──────────────────────────────────────────────────

export interface PanelPeerOptions {
  panelId: string;
  source: PanelVideoSource;
  config: WebrtcConfig;
  /** Send a signaling message to THIS subscriber over the panel WS. */
  send: (msg: PanelMessage) => void;
  /** Called whenever this subscriber's effective transport changes. */
  onTransport: (decision: TransportDecision) => void;
}

/** RTP packets a peer must have carried before we call it "media flowing".
 *  A handful, not one: a single packet can be in flight when a path is about to
 *  fail, and declaring WebRTC live would mute the fallback prematurely. */
const MEDIA_FLOWING_PACKETS = 8;

/**
 * One subscriber's peer connection.
 *
 * Owns the offer/answer/ICE exchange, the negotiation deadline, and the
 * transport verdict for that subscriber. It never mutes the screencast itself —
 * it reports a decision and the session acts on it, so the fallback logic lives
 * in exactly one place.
 */
export class PanelPeer {
  private pc: WeriftPc | null = null;
  private track: WeriftTrack | null = null;
  private detach: (() => void) | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private packetsSent = 0;
  private mediaFlowing = false;
  private failure: string | undefined;
  private closed = false;
  private readonly candidateTypes = new Map<string, number>();
  private readonly logger;

  /** The transport this subscriber is currently on. */
  transport: TransportDecision = { transport: "screencast", reason: "webrtc not started" };

  constructor(private readonly opts: PanelPeerOptions) {
    this.logger = log.child({ panel_id: opts.panelId });
  }

  /**
   * Build the peer connection and send the offer.
   *
   * Any failure here — werift missing, no display, ffmpeg gone — resolves to a
   * screencast verdict rather than an exception: the caller has a working panel
   * either way and must not have to handle two outcomes.
   */
  async start(): Promise<void> {
    try {
      const werift = await loadWerift();
      const codec = codecParameters(werift, this.opts.config.encoder);
      const pc = new werift.RTCPeerConnection({
        codecs: { video: [codec] },
        iceUseTcp: this.opts.config.network.iceUseTcp,
        ...(this.opts.config.network.iceHostAddresses.length ? { iceAdditionalHostAddresses: this.opts.config.network.iceHostAddresses } : {}),
        ...(this.opts.config.network.icePortRange ? { icePortRange: this.opts.config.network.icePortRange } : {}),
        ...(this.opts.config.network.iceServers.length ? { iceServers: this.opts.config.network.iceServers } : {}),
      }) as unknown as WeriftPc;
      this.pc = pc;

      const track = new werift.MediaStreamTrack({ kind: "video" }) as unknown as WeriftTrack;
      this.track = track;
      pc.addTransceiver(track, { direction: "sendonly" });

      pc.onIceCandidate.subscribe((candidate) => {
        if (this.closed) return;
        if (!candidate) {
          this.opts.send({ type: "ice", candidate: null });
          return;
        }
        const desc = describeCandidate(candidate.candidate);
        const key = `${desc.type}/${desc.protocol}`;
        this.candidateTypes.set(key, (this.candidateTypes.get(key) ?? 0) + 1);
        this.opts.send({
          type: "ice",
          candidate: {
            candidate: announceCandidate(candidate.candidate, this.opts.config.network.announceIp),
            sdpMid: candidate.sdpMid ?? null,
            sdpMLineIndex: candidate.sdpMLineIndex ?? null,
          },
        });
      });

      pc.connectionStateChange.subscribe((state) => {
        void this.onConnectionState(state);
      });

      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      const sdp = announceSdp(pc.localDescription?.sdp ?? offer.sdp, this.opts.config.network.announceIp);
      this.opts.send({ type: "offer", sdp });
      this.logger.info("panel webrtc offer sent", { codec: this.opts.config.encoder.codec, ice_tcp: this.opts.config.network.iceUseTcp });

      // The fallback deadline. It starts at the OFFER, not at connect, so a
      // client that never answers is failed over just like one whose UDP is
      // dropped.
      this.timer = setTimeout(() => {
        if (this.mediaFlowing || this.closed) return;
        this.logger.warn("panel webrtc negotiation timed out — falling back", {
          state: this.pc?.connectionState ?? "none",
          candidates: Object.fromEntries(this.candidateTypes),
        });
        this.publish({ timedOut: true });
      }, this.opts.config.negotiationTimeoutMs);
      if (typeof this.timer.unref === "function") this.timer.unref();
    } catch (err) {
      this.failure = (err as Error).message;
      this.logger.warn("panel webrtc start failed — screencast only", { detail: this.failure });
      this.publish({});
    }
  }

  /** Apply the client's answer. */
  async acceptAnswer(sdp: string): Promise<void> {
    if (!this.pc || this.closed) return;
    try {
      await this.pc.setRemoteDescription({ type: "answer", sdp });
    } catch (err) {
      this.failure = `bad answer: ${(err as Error).message}`;
      this.logger.warn("panel webrtc answer rejected", { detail: (err as Error).message });
      this.publish({});
    }
  }

  /** Apply one trickled remote candidate (null = end-of-candidates). */
  async acceptCandidate(candidate: PanelIceCandidate | null): Promise<void> {
    if (!this.pc || this.closed) return;
    try {
      await this.pc.addIceCandidate(candidate ?? null);
    } catch (err) {
      // A candidate that arrives before the remote description, or for a
      // component we did not offer, is normal trickle noise — never fatal.
      this.logger.debug("panel webrtc candidate ignored", { detail: (err as Error).message });
    }
  }

  private async onConnectionState(state: string): Promise<void> {
    if (this.closed) return;
    this.logger.info("panel webrtc state", { state, candidates: Object.fromEntries(this.candidateTypes) });
    if (state === "connected") {
      try {
        // The encoder starts HERE, not at offer time: no peer, no encode.
        this.detach = await this.opts.source.attach((packet) => this.writeRtp(packet));
      } catch (err) {
        this.failure = (err as Error).message;
        this.logger.warn("panel encoder unavailable — screencast only", { detail: this.failure });
        this.publish({});
      }
      return;
    }
    if (state === "failed" || state === "closed" || state === "disconnected") {
      this.failure = `peer ${state}`;
      this.publish({});
    }
  }

  private writeRtp(packet: Buffer): void {
    if (this.closed || !this.track) return;
    try {
      this.track.writeRtp(packet);
    } catch (err) {
      this.logger.warn("panel rtp write failed", { detail: (err as Error).message });
      return;
    }
    this.packetsSent++;
    if (!this.mediaFlowing && this.packetsSent >= MEDIA_FLOWING_PACKETS) {
      this.mediaFlowing = true;
      this.publish({});
    }
  }

  /** Recompute and, on change, announce this subscriber's transport. */
  private publish(extra: { timedOut?: boolean }): void {
    const next = decideTransport({
      clientWantsWebrtc: true,
      serverCanWebrtc: true,
      mediaFlowing: this.mediaFlowing,
      timedOut: extra.timedOut === true,
      failure: this.failure,
    });
    if (next.transport === this.transport.transport && next.reason === this.transport.reason) return;
    this.transport = next;
    this.opts.send({ type: "transport", transport: next.transport, reason: next.reason });
    this.opts.onTransport(next);
    this.logger.info("panel transport", { transport: next.transport, reason: next.reason, packets: this.packetsSent });
  }

  /** Live counters for the client's stats readout and the pod's logs. */
  stats(): { packetsSent: number; transport: string } {
    return { packetsSent: this.packetsSent, transport: this.transport.transport };
  }

  /** Tear the peer down. Idempotent; never throws. */
  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    try { this.detach?.(); } catch { /* already detached */ }
    this.detach = null;
    try { this.track?.stop(); } catch { /* already stopped */ }
    this.track = null;
    const pc = this.pc;
    this.pc = null;
    if (pc) await pc.close().catch(() => { /* already closed */ });
  }
}

// ── werift loading + codec description ───────────────────────────────────────

interface WeriftModule {
  RTCPeerConnection: new (config: Record<string, unknown>) => unknown;
  MediaStreamTrack: new (props: { kind: string }) => unknown;
  RTCRtpCodecParameters: new (props: Record<string, unknown>) => unknown;
}

let weriftModule: Promise<WeriftModule> | null = null;

/** Load werift on first use. Dynamic so the rotor/harness modes never pay for
 *  it, and so its absence degrades to screencast instead of failing the pod. */
async function loadWerift(): Promise<WeriftModule> {
  if (!weriftModule) {
    weriftModule = import("werift").then((m) => m as unknown as WeriftModule);
  }
  return weriftModule;
}

/**
 * The SDP description of what ffmpeg is producing.
 *
 * `profile-level-id=42e01f` is constrained-baseline level 3.1, which is exactly
 * what `-profile:v baseline` + `-tune zerolatency` emits (no B-frames, no
 * CABAC) and is the profile every browser decodes. `packetization-mode=1` matches
 * ffmpeg's RTP muxer, which uses fragmentation units for large NALs. NACK/PLI are
 * advertised because a dropped packet on a long-keyint stream is worth repairing.
 */
function codecParameters(werift: WeriftModule, cfg: EncoderConfig): unknown {
  if (cfg.codec === "vp8") {
    return new werift.RTCRtpCodecParameters({
      mimeType: "video/VP8",
      clockRate: 90000,
      payloadType: cfg.payloadType,
      rtcpFeedback: [{ type: "nack" }, { type: "nack", parameter: "pli" }, { type: "goog-remb" }],
    });
  }
  return new werift.RTCRtpCodecParameters({
    mimeType: "video/H264",
    clockRate: 90000,
    payloadType: cfg.payloadType,
    rtcpFeedback: [{ type: "nack" }, { type: "nack", parameter: "pli" }, { type: "goog-remb" }],
    parameters: "packetization-mode=1;level-asymmetry-allowed=1;profile-level-id=42e01f",
  });
}

/** Whether this pod could do WebRTC at all: werift importable AND ffmpeg
 *  runnable. The display check is per-panel (a page may or may not have one), so
 *  it is not part of this probe. */
export async function webrtcAvailable(ffmpegPath?: string): Promise<boolean> {
  try {
    await loadWerift();
  } catch (err) {
    log.warn("panel webrtc unavailable — werift did not load", { detail: (err as Error).message });
    return false;
  }
  return hasFfmpeg(ffmpegPath);
}
