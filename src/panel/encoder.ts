/**
 * panel/encoder.ts — NATIVE video encode for a browser panel: ffmpeg grabs the
 * panel's X display and emits RTP; nothing is decoded or encoded in JS.
 *
 * This is the load-bearing decision of the v2 video path. The v1 path pulled
 * base64 JPEGs out of CDP and shipped them over the WS — ~18KB per frame, ~4Mbps
 * per active panel, and every byte crossed the Node heap. Here Chromium paints
 * into a real X display (Xvfb, headful — panel/xvfb.ts), `ffmpeg -f x11grab`
 * reads that display directly, libx264/libvpx encodes it in native code, and
 * ffmpeg's RTP muxer hands us packets that are already the exact shape werift
 * needs. Node's only job is to move ~1200-byte datagrams from a loopback socket
 * into a peer connection — it never sees a pixel.
 *
 * Layout:
 *   {@link ffmpegArgs}  PURE — the whole encoder configuration as argv. Unit
 *                       tested; every tuning decision (zerolatency, baseline,
 *                       keyint, bitrate cap, pkt_size) is visible in one place.
 *   {@link X11Encoder}  the process + the loopback RTP socket pair, with byte
 *                       accounting so the panel can report a real bitrate.
 *
 * Why RTP over loopback rather than ffmpeg's stdout: ffmpeg's RTP muxer already
 * does correct H.264/VP8 payloadization (fragmentation units, marker bits,
 * timestamps). Re-deriving that in TypeScript would be the one genuinely
 * error-prone part of this pipeline, and it would put per-packet work back in
 * JS. We bind BOTH the RTP port and its RTCP sibling (port+1) — ffmpeg connects
 * its RTCP socket too, and on Linux a datagram to a closed local port returns
 * ECONNREFUSED, which would kill the muxer.
 *
 * Secret hygiene: ffmpeg argv contains only a display specifier and a loopback
 * URL — no urls, no tokens. stderr is surfaced at warn level (it is where a real
 * encoder failure shows up) and is never suppressed.
 */

import { spawn } from "node:child_process";
import type { ChildProcess } from "node:child_process";
import * as dgram from "node:dgram";

import { log } from "../obs/logger.js";

/** Codecs this encoder can produce, most-preferred first. */
export type VideoCodec = "h264" | "vp8";

/** Where on which X display the panel's pixels live. */
export interface CaptureTarget {
  /** X display specifier, e.g. `":99"`. */
  display: string;
  /** Top-left of the page's CONTENT rect in physical display pixels. */
  x: number;
  y: number;
  /** Size of the content rect in physical display pixels (CSS px × DPR). */
  width: number;
  height: number;
}

export interface EncoderConfig {
  codec: VideoCodec;
  /** Capture/encode framerate. */
  fps: number;
  /**
   * Bitrate CEILING in kbit/s — a VBV cap, not a target.
   *
   * This distinction is the single biggest bandwidth finding of the spike. Given
   * `-b:v N` libx264 runs average-bitrate rate control and PADS a static page up
   * to N: a completely still screen cost ~480 kbit/s, which is most of the win
   * thrown away for nothing. Quality-driven encoding (`-crf`) with this value as
   * `-maxrate` instead lets an idle page cost almost nothing while a pathological
   * one still cannot exceed the budget this whole exercise exists to defend.
   */
  bitrateKbps: number;
  /** Constant-rate-factor quality, 0 (lossless) – 51. Lower is better and
   *  bigger. Screen content is mostly flat colour and text, so it sits far below
   *  the ceiling at a quality that keeps small type crisp. */
  crf: number;
  /**
   * Frames between keyframes. This is the JOIN-LATENCY vs IDLE-BANDWIDTH dial
   * and the single most consequential number here: a keyframe is orders of
   * magnitude larger than a static-page delta frame, so a short interval taxes
   * every idle panel forever to save a late joiner one wait. We keep it long and
   * instead start the encoder ON DEMAND (the first WebRTC subscriber's connect
   * IS a keyframe), which makes the common case — one viewer per panel — pay
   * nothing for joins.
   */
  keyint: number;
  /** RTP payload type; must match what the peer connection advertises. */
  payloadType: number;
  /** Max RTP payload size — 1200 keeps a packet under a 1500-byte MTU once SRTP
   *  and UDP/IP overhead are added. */
  pktSize: number;
}

export const DEFAULT_ENCODER: EncoderConfig = {
  codec: "h264",
  fps: 30,
  bitrateKbps: 2500,
  crf: 26,
  keyint: 300,
  payloadType: 96,
  pktSize: 1200,
};

/** RTP payload type per codec — fixed so the SDP and the ffmpeg muxer agree. */
export const PAYLOAD_TYPES: Record<VideoCodec, number> = { h264: 96, vp8: 97 };

/**
 * Build the ffmpeg argv for one panel. PURE — no spawn, no filesystem, no env.
 *
 * x11grab reads the display directly; `-draw_mouse 0` because the panel's cursor
 * is virtual (input is dispatched over CDP, the X pointer never moves, so drawing
 * it would paint a stale arrow at the origin of every stream).
 */
export function ffmpegArgs(cfg: EncoderConfig, target: CaptureTarget, rtpPort: number): string[] {
  const width = evenDim(target.width);
  const height = evenDim(target.height);
  const args = [
    "-hide_banner",
    "-loglevel", "error",
    "-nostdin",
    // ── input: the panel's rect of the X display ──
    "-f", "x11grab",
    "-draw_mouse", "0",
    "-framerate", String(cfg.fps),
    "-video_size", `${width}x${height}`,
    "-i", `${target.display}+${Math.max(0, Math.round(target.x))},${Math.max(0, Math.round(target.y))}`,
    "-an",
  ];

  if (cfg.codec === "h264") {
    args.push(
      "-c:v", "libx264",
      // Baseline + zerolatency: no B-frames, no lookahead, no frame reordering —
      // the encoder emits a frame as soon as it has one, which is the whole point
      // for an interactive panel.
      "-profile:v", "baseline",
      "-preset", "ultrafast",
      "-tune", "zerolatency",
      "-pix_fmt", "yuv420p",
      // Quality-driven, ceiling-bounded — see EncoderConfig.bitrateKbps.
      "-crf", String(cfg.crf),
      // repeat-headers puts SPS/PPS in front of every IDR, so a peer that joins
      // (or recovers) can decode from the next keyframe without side-channel
      // parameter sets. scenecut=0 stops the encoder inserting unbudgeted
      // keyframes when a page repaints wholesale (a scroll looks like a cut).
      "-x264-params", `keyint=${cfg.keyint}:min-keyint=${cfg.keyint}:scenecut=0:repeat-headers=1`,
    );
  } else {
    args.push(
      "-c:v", "libvpx",
      "-deadline", "realtime",
      "-cpu-used", "8",
      "-pix_fmt", "yuv420p",
      "-g", String(cfg.keyint),
      "-error-resilient", "1",
      // libvpx buffers by default; without this a low-motion page can sit on a
      // frame for seconds before it is emitted.
      "-lag-in-frames", "0",
      // libvpx's constrained-quality mode: -crf is the quality, -b:v the ceiling.
      "-crf", String(cfg.crf),
      "-qmin", "4",
      "-qmax", "50",
    );
  }

  args.push(
    // The CEILING, on both codecs. libx264 reads maxrate+bufsize as VBV; libvpx
    // reads -b:v as the cap in CQ mode.
    "-b:v", `${cfg.bitrateKbps}k`,
    "-maxrate", `${cfg.bitrateKbps}k`,
    "-bufsize", `${cfg.bitrateKbps * 2}k`,
    "-f", "rtp",
    "-payload_type", String(cfg.payloadType),
    `rtp://127.0.0.1:${rtpPort}?pkt_size=${cfg.pktSize}`,
  );
  return args;
}

/** H.264 and VP8 both need even dimensions for 4:2:0 chroma; an odd viewport
 *  would make ffmpeg fail at startup rather than at runtime. */
function evenDim(n: number): number {
  const v = Math.max(2, Math.round(Number.isFinite(n) ? n : 2));
  return v % 2 === 0 ? v : v - 1;
}

/** Bind an even RTP port together with its RTCP sibling. Both sockets are held
 *  for the encoder's lifetime; the RTCP one exists purely so ffmpeg's RTCP
 *  sender does not hit a closed port. */
async function bindRtpPair(range: [number, number]): Promise<{ rtp: dgram.Socket; rtcp: dgram.Socket; port: number }> {
  const [lo, hi] = range;
  const span = Math.max(2, hi - lo);
  for (let attempt = 0; attempt < 24; attempt++) {
    const port = lo + 2 * Math.floor((Math.random() * span) / 2);
    const rtp = dgram.createSocket("udp4");
    const rtcp = dgram.createSocket("udp4");
    try {
      await bindSocket(rtp, port);
      await bindSocket(rtcp, port + 1);
      return { rtp, rtcp, port };
    } catch {
      rtp.close();
      try { rtcp.close(); } catch { /* never bound */ }
    }
  }
  throw new Error("no free RTP port pair for the panel encoder");
}

function bindSocket(sock: dgram.Socket, port: number): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const onError = (err: Error): void => { sock.off("listening", onListening); reject(err); };
    const onListening = (): void => { sock.off("error", onError); resolve(); };
    sock.once("error", onError);
    sock.once("listening", onListening);
    sock.bind(port, "127.0.0.1");
  });
}

export interface X11EncoderOptions {
  config: EncoderConfig;
  target: CaptureTarget;
  /** ffmpeg binary; defaults to `ffmpeg` on PATH (PANEL_FFMPEG_PATH overrides). */
  ffmpegPath?: string;
  /** Local UDP port range to draw the loopback RTP pair from. */
  portRange?: [number, number];
  /** Called for every RTP packet ffmpeg produces. */
  onRtp: (packet: Buffer) => void;
  /** Called once when the encoder dies unexpectedly. */
  onExit?: (reason: string) => void;
  logFields?: Record<string, unknown>;
}

/**
 * One ffmpeg x11grab → RTP encoder for one panel.
 *
 * Lifecycle is strictly start/stop and idempotent in both directions: the panel
 * starts an encoder when a subscriber's peer connection comes up and stops it
 * when the last one goes away, so an idle panel costs ZERO encoder CPU. That
 * on-demand shape is also what lets the keyframe interval stay long (see
 * {@link EncoderConfig.keyint}).
 */
export class X11Encoder {
  private proc: ChildProcess | null = null;
  private rtp: dgram.Socket | null = null;
  private rtcp: dgram.Socket | null = null;
  private stopped = false;
  private readonly logger;

  /** Cumulative RTP bytes emitted — the honest wire cost of this panel's video
   *  (payload only; SRTP/UDP/IP overhead is added by the peer connection). */
  bytesOut = 0;
  /** Cumulative RTP packets emitted. */
  packetsOut = 0;
  /** When the first packet appeared — the encoder's true time-to-first-frame. */
  firstPacketAt = 0;
  readonly startedAt = Date.now();

  constructor(private readonly opts: X11EncoderOptions) {
    this.logger = log.child(opts.logFields ?? {});
  }

  /** Spawn ffmpeg and start pumping RTP. Rejects if the binary cannot start. */
  async start(): Promise<void> {
    if (this.proc || this.stopped) return;
    const { rtp, rtcp, port } = await bindRtpPair(this.opts.portRange ?? [41000, 41400]);
    this.rtp = rtp;
    this.rtcp = rtcp;
    rtp.on("message", (msg) => {
      if (this.stopped) return;
      this.bytesOut += msg.length;
      this.packetsOut++;
      if (!this.firstPacketAt) {
        this.firstPacketAt = Date.now();
        this.logger.info("panel encoder first packet", { ms: this.firstPacketAt - this.startedAt });
      }
      try {
        this.opts.onRtp(msg);
      } catch (err) {
        this.logger.warn("panel rtp sink failed", { detail: (err as Error).message });
      }
    });
    rtp.on("error", (err) => this.logger.warn("panel rtp socket error", { detail: err.message }));
    rtcp.on("error", () => { /* we never read RTCP; the socket exists to absorb it */ });

    const bin = this.opts.ffmpegPath || "ffmpeg";
    const args = ffmpegArgs(this.opts.config, this.opts.target, port);
    this.logger.info("panel encoder starting", {
      codec: this.opts.config.codec,
      fps: this.opts.config.fps,
      size: `${this.opts.target.width}x${this.opts.target.height}`,
      bitrate_kbps: this.opts.config.bitrateKbps,
      rtp_port: port,
    });

    let proc: ChildProcess;
    try {
      proc = spawn(bin, args, { stdio: ["ignore", "ignore", "pipe"] });
    } catch (err) {
      await this.stop();
      throw new Error(`could not start the video encoder: ${(err as Error).message}`);
    }
    this.proc = proc;

    proc.stderr?.setEncoding("utf8");
    proc.stderr?.on("data", (chunk: string) => {
      const line = chunk.trim();
      if (line) this.logger.warn("panel encoder stderr", { detail: line.slice(0, 400) });
    });
    proc.on("error", (err) => {
      this.logger.error("panel encoder spawn failed", { detail: err.message });
      this.opts.onExit?.(`encoder spawn failed: ${err.message}`);
    });
    proc.on("exit", (code, signal) => {
      this.proc = null;
      if (this.stopped) return;
      const reason = `encoder exited (code=${code ?? "null"} signal=${signal ?? "null"})`;
      this.logger.warn("panel encoder exited", { code, signal });
      this.opts.onExit?.(reason);
    });
  }

  /** Kill ffmpeg and release both sockets. Idempotent; never throws. */
  async stop(): Promise<void> {
    if (this.stopped) return;
    this.stopped = true;
    const proc = this.proc;
    this.proc = null;
    if (proc && proc.exitCode === null) {
      try {
        proc.kill("SIGTERM");
        // ffmpeg exits promptly on SIGTERM; SIGKILL is the backstop so a wedged
        // encoder can never outlive its panel.
        await new Promise<void>((resolve) => {
          const t = setTimeout(() => { try { proc.kill("SIGKILL"); } catch { /* gone */ } resolve(); }, 2000);
          proc.once("exit", () => { clearTimeout(t); resolve(); });
        });
      } catch {
        /* already dead */
      }
    }
    for (const sock of [this.rtp, this.rtcp]) {
      try { sock?.close(); } catch { /* already closed */ }
    }
    this.rtp = null;
    this.rtcp = null;
    this.logger.info("panel encoder stopped", { bytes_out: this.bytesOut, packets_out: this.packetsOut });
  }
}

/** Whether an ffmpeg binary is actually runnable. Cached — this is asked once
 *  per capability probe, not per panel. */
let ffmpegProbe: Promise<boolean> | null = null;

export function hasFfmpeg(ffmpegPath?: string): Promise<boolean> {
  if (ffmpegProbe) return ffmpegProbe;
  ffmpegProbe = new Promise<boolean>((resolve) => {
    try {
      const proc = spawn(ffmpegPath || "ffmpeg", ["-hide_banner", "-version"], { stdio: "ignore" });
      proc.on("error", () => resolve(false));
      proc.on("exit", (code) => resolve(code === 0));
    } catch {
      resolve(false);
    }
  });
  return ffmpegProbe;
}

/** Test seam — forget the cached ffmpeg probe. */
export function resetFfmpegProbe(): void {
  ffmpegProbe = null;
}
