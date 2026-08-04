/**
 * The v2 WebRTC video transport's PURE seams, with no peer connection, no
 * ffmpeg and no Chromium anywhere near the process.
 *
 * Three things are worth testing without hardware and they are exactly the three
 * things that can silently break the panel:
 *   • the SIGNALING mapping — what the wire is allowed to say, and what a
 *     hostile/garbled frame does (drop, never throw);
 *   • the TRANSPORT decision — every path through it must end at `screencast`
 *     except the one that has proven media, because that rule IS the mandatory
 *     fallback;
 *   • the ENCODER ARGV — the ffmpeg configuration is a long string of tuning
 *     that no runtime assertion would ever catch being wrong.
 *
 * Plus the session-level fallback: a client that asks for WebRTC on a pod that
 * cannot serve it must be told so and must keep receiving JPEG frames.
 */

import { describe, it, expect } from "vitest";

import { ffmpegArgs, DEFAULT_ENCODER, PAYLOAD_TYPES } from "../../src/panel/encoder.js";
import type { CaptureTarget } from "../../src/panel/encoder.js";
import { parseSignal, describeCandidate, decideTransport, screencastNeeded, isSignalType, announceCandidate, announceSdp } from "../../src/panel/signal.js";
import { parsePortRange, parseIceServers, webrtcConfigFromEnv } from "../../src/panel/webrtc.js";
import { parseScreen, DEFAULT_SCREEN } from "../../src/panel/xvfb.js";
import { PanelSession } from "../../src/panel/session.js";
import type { PanelPage } from "../../src/panel/browser.js";
import type { PanelMessage } from "../../src/panel/frames.js";

// ── encoder argv ─────────────────────────────────────────────────────────────

const TARGET: CaptureTarget = { display: ":99", x: 0, y: 74, width: 1024, height: 720 };

describe("ffmpegArgs — the native encoder configuration", () => {
  it("grabs the panel's rect of the X display and muxes RTP to loopback", () => {
    const args = ffmpegArgs(DEFAULT_ENCODER, TARGET, 41002);
    const joined = args.join(" ");
    expect(joined).toContain("-f x11grab");
    expect(joined).toContain("-video_size 1024x720");
    expect(joined).toContain("-i :99+0,74");
    // The cursor is virtual (input comes over CDP) — drawing it would paint a
    // stale arrow into every stream.
    expect(joined).toContain("-draw_mouse 0");
    expect(args[args.length - 1]).toBe("rtp://127.0.0.1:41002?pkt_size=1200");
    expect(joined).toContain("-payload_type 96");
  });

  it("encodes H.264 baseline with zerolatency and a bounded bitrate", () => {
    const joined = ffmpegArgs(DEFAULT_ENCODER, TARGET, 41002).join(" ");
    expect(joined).toContain("-c:v libx264");
    expect(joined).toContain("-profile:v baseline");
    expect(joined).toContain("-tune zerolatency");
    expect(joined).toContain(`keyint=${DEFAULT_ENCODER.keyint}:min-keyint=${DEFAULT_ENCODER.keyint}`);
    expect(joined).toContain("repeat-headers=1");
    // Capped, not CRF: a pathological page cannot blow the bandwidth budget.
    expect(joined).toContain("-maxrate 2500k");
  });

  it("encodes VP8 in realtime mode with no lag when asked", () => {
    const joined = ffmpegArgs({ ...DEFAULT_ENCODER, codec: "vp8", payloadType: PAYLOAD_TYPES.vp8 }, TARGET, 41010).join(" ");
    expect(joined).toContain("-c:v libvpx");
    expect(joined).toContain("-deadline realtime");
    expect(joined).toContain("-lag-in-frames 0");
    expect(joined).toContain("-payload_type 97");
    expect(joined).not.toContain("libx264");
  });

  it("rounds odd dimensions down — 4:2:0 chroma cannot encode them", () => {
    const joined = ffmpegArgs(DEFAULT_ENCODER, { ...TARGET, width: 1023, height: 721 }, 41004).join(" ");
    expect(joined).toContain("-video_size 1022x720");
  });

  it("never emits a negative capture origin", () => {
    const joined = ffmpegArgs(DEFAULT_ENCODER, { ...TARGET, x: -5, y: -9 }, 41006).join(" ");
    expect(joined).toContain("-i :99+0,0");
  });
});

// ── signaling ────────────────────────────────────────────────────────────────

describe("parseSignal — the client→server signaling vocabulary", () => {
  it("recognises the three signaling verbs and nothing else", () => {
    expect(isSignalType("hello")).toBe(true);
    expect(isSignalType("answer")).toBe(true);
    expect(isSignalType("ice")).toBe(true);
    expect(isSignalType("mouse")).toBe(false);
    expect(isSignalType(undefined)).toBe(false);
  });

  it("parses a hello, defaulting webrtc to false unless asked for explicitly", () => {
    expect(parseSignal({ type: "hello", webrtc: true, codecs: ["h264", "vp8"] })).toEqual({ type: "hello", webrtc: true, codecs: ["h264", "vp8"] });
    expect(parseSignal({ type: "hello" })).toEqual({ type: "hello", webrtc: false });
    // "truthy" is not "true" — an accidental string must not opt a client in.
    expect(parseSignal({ type: "hello", webrtc: "yes" })).toEqual({ type: "hello", webrtc: false });
  });

  it("accepts a well-formed answer and rejects anything that is not SDP", () => {
    expect(parseSignal({ type: "answer", sdp: "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\n" })).toMatchObject({ type: "answer" });
    expect(parseSignal({ type: "answer", sdp: "not sdp" })).toBeNull();
    expect(parseSignal({ type: "answer" })).toBeNull();
    expect(parseSignal({ type: "answer", sdp: `v=0${"x".repeat(70_000)}` })).toBeNull();
  });

  it("parses a trickled candidate and normalises every spelling of end-of-candidates", () => {
    const parsed = parseSignal({ type: "ice", candidate: { candidate: "candidate:1 1 udp 2113937151 10.0.0.5 51000 typ host", sdpMid: "0", sdpMLineIndex: 0 } });
    expect(parsed).toMatchObject({ type: "ice", candidate: { sdpMid: "0", sdpMLineIndex: 0 } });
    expect(parseSignal({ type: "ice", candidate: null })).toEqual({ type: "ice", candidate: null });
    expect(parseSignal({ type: "ice" })).toEqual({ type: "ice", candidate: null });
    expect(parseSignal({ type: "ice", candidate: { candidate: "" } })).toEqual({ type: "ice", candidate: null });
  });

  it("drops garbage rather than throwing — a WS peer can send anything", () => {
    expect(parseSignal(null)).toBeNull();
    expect(parseSignal("hello")).toBeNull();
    expect(parseSignal({ type: "offer", sdp: "v=0" })).toBeNull();   // server→client only
    expect(parseSignal({ type: "ice", candidate: { candidate: "x".repeat(2000) } })).toBeNull();
  });
});

describe("describeCandidate — loggable without leaking an address", () => {
  it("extracts only the type and protocol", () => {
    expect(describeCandidate("candidate:1 1 udp 2113937151 10.0.0.5 51000 typ host")).toEqual({ type: "host", protocol: "udp" });
    expect(describeCandidate("candidate:2 1 tcp 1518280447 10.0.0.5 9 typ srflx raddr 0.0.0.0")).toEqual({ type: "srflx", protocol: "tcp" });
    expect(describeCandidate("nonsense")).toEqual({ type: "unknown", protocol: "unknown" });
  });
});

describe("announceCandidate — the NAT knob", () => {
  const host = "candidate:1 1 udp 2113937151 172.17.0.2 41502 typ host generation 0";

  it("rewrites a host candidate's address to what the client can reach", () => {
    expect(announceCandidate(host, "203.0.113.7")).toBe("candidate:1 1 udp 2113937151 203.0.113.7 41502 typ host generation 0");
  });

  it("leaves srflx and relay candidates alone — their address is already external", () => {
    const srflx = "candidate:2 1 udp 1677729535 198.51.100.4 41502 typ srflx raddr 172.17.0.2 rport 41502";
    expect(announceCandidate(srflx, "203.0.113.7")).toBe(srflx);
  });

  it("is a no-op with no announce address configured, and survives an a= prefix", () => {
    expect(announceCandidate(host, "")).toBe(host);
    expect(announceCandidate("a=" + host, "203.0.113.7")).toBe("a=candidate:1 1 udp 2113937151 203.0.113.7 41502 typ host generation 0");
    expect(announceCandidate("garbage", "203.0.113.7")).toBe("garbage");
  });

  it("rewrites candidates embedded in an offer too, so the client never races to a dead address", () => {
    const sdp = ["v=0", "m=video 9 UDP/TLS/RTP/SAVPF 96", "a=" + host, "a=mid:0"].join("\r\n");
    const out = announceSdp(sdp, "203.0.113.7");
    expect(out).toContain("203.0.113.7");
    expect(out).not.toContain("172.17.0.2");
    expect(out).toContain("a=mid:0");
    expect(announceSdp(sdp, "")).toBe(sdp);
  });
});

// ── the fallback rule ────────────────────────────────────────────────────────

describe("decideTransport — screencast is the floor", () => {
  const base = { clientWantsWebrtc: true, serverCanWebrtc: true, mediaFlowing: false, timedOut: false };

  it("only returns webrtc when media is actually flowing", () => {
    expect(decideTransport({ ...base, mediaFlowing: true }).transport).toBe("webrtc");
  });

  it("falls back when the client never asked", () => {
    expect(decideTransport({ ...base, clientWantsWebrtc: false, mediaFlowing: true })).toMatchObject({ transport: "screencast", reason: expect.stringContaining("did not request") });
  });

  it("falls back when the pod cannot serve it", () => {
    expect(decideTransport({ ...base, serverCanWebrtc: false })).toMatchObject({ transport: "screencast", reason: expect.stringContaining("no capturable display") });
  });

  it("falls back on a timeout", () => {
    expect(decideTransport({ ...base, timedOut: true })).toMatchObject({ transport: "screencast", reason: expect.stringContaining("timed out") });
  });

  it("a failure OVERRIDES flowing media — a dead peer must not stay selected", () => {
    expect(decideTransport({ ...base, mediaFlowing: true, failure: "peer failed" })).toMatchObject({ transport: "screencast", reason: expect.stringContaining("peer failed") });
  });

  it("reports 'negotiating' while it is still trying, and that is still screencast", () => {
    expect(decideTransport(base)).toMatchObject({ transport: "screencast", reason: "webrtc negotiating" });
  });
});

describe("screencastNeeded — when the JPEG path may be switched off", () => {
  it("stays on while any subscriber is still on it", () => {
    expect(screencastNeeded(["webrtc", "screencast"])).toBe(true);
    expect(screencastNeeded(["screencast"])).toBe(true);
  });
  it("switches off only when every subscriber has landed on webrtc", () => {
    expect(screencastNeeded(["webrtc"])).toBe(false);
    expect(screencastNeeded(["webrtc", "webrtc"])).toBe(false);
  });
  it("stays on with no subscribers — v1's lifecycle is unchanged", () => {
    expect(screencastNeeded([])).toBe(true);
  });
});

// ── config parsing ───────────────────────────────────────────────────────────

describe("webrtc network config", () => {
  it("parses a port range and rejects a degenerate one", () => {
    expect(parsePortRange("41500-41600")).toEqual([41500, 41600]);
    expect(parsePortRange("41500-41500")).toBeUndefined();
    expect(parsePortRange("80-90")).toBeUndefined();      // privileged
    expect(parsePortRange("nope")).toBeUndefined();
    expect(parsePortRange(undefined)).toBeUndefined();
  });

  it("parses stun and credentialed turn urls", () => {
    expect(parseIceServers("stun:stun.example:3478")).toEqual([{ urls: "stun:stun.example:3478" }]);
    expect(parseIceServers("turn:user:secret@turn.example:3478")).toEqual([{ urls: "turn:turn.example:3478", username: "user", credential: "secret" }]);
    expect(parseIceServers("")).toEqual([]);
  });

  it("builds a config from env, with laptop-sane defaults", () => {
    const cfg = webrtcConfigFromEnv({});
    expect(cfg.encoder.codec).toBe("h264");
    expect(cfg.encoder.payloadType).toBe(PAYLOAD_TYPES.h264);
    expect(cfg.network.iceUseTcp).toBe(false);
    expect(cfg.negotiationTimeoutMs).toBeGreaterThan(1000);

    const tuned = webrtcConfigFromEnv({
      PANEL_WEBRTC_CODEC: "vp8",
      PANEL_WEBRTC_FPS: "24",
      PANEL_WEBRTC_BITRATE_KBPS: "1500",
      PANEL_ICE_TCP: "1",
      PANEL_ICE_HOST_IPS: "203.0.113.7, 198.51.100.9",
      PANEL_ICE_PORT_RANGE: "41500-41600",
    });
    expect(tuned.encoder.codec).toBe("vp8");
    expect(tuned.encoder.payloadType).toBe(PAYLOAD_TYPES.vp8);
    expect(tuned.encoder.fps).toBe(24);
    expect(tuned.encoder.bitrateKbps).toBe(1500);
    expect(tuned.network.iceUseTcp).toBe(true);
    expect(tuned.network.iceHostAddresses).toEqual(["203.0.113.7", "198.51.100.9"]);
    expect(tuned.network.icePortRange).toEqual([41500, 41600]);
  });

  it("clamps a nonsense framerate rather than handing it to ffmpeg", () => {
    expect(webrtcConfigFromEnv({ PANEL_WEBRTC_FPS: "999" }).encoder.fps).toBe(60);
    expect(webrtcConfigFromEnv({ PANEL_WEBRTC_FPS: "-1" }).encoder.fps).toBe(DEFAULT_ENCODER.fps);
  });
});

describe("parseScreen — the virtual framebuffer geometry", () => {
  it("takes a sane WxH and falls back on anything else", () => {
    expect(parseScreen("1920x1080", DEFAULT_SCREEN)).toEqual({ width: 1920, height: 1080 });
    expect(parseScreen("tiny", DEFAULT_SCREEN)).toEqual(DEFAULT_SCREEN);
    expect(parseScreen("100x100", DEFAULT_SCREEN)).toEqual(DEFAULT_SCREEN);
    expect(parseScreen(undefined, DEFAULT_SCREEN)).toEqual(DEFAULT_SCREEN);
  });
});

// ── the session-level fallback ───────────────────────────────────────────────

/** A fake CDP page. `capturable` decides whether it pretends to have a display. */
class FakePage implements PanelPage {
  readonly sent: Array<{ method: string; params: Record<string, unknown> }> = [];
  private handlers = new Map<string, Set<(p: any) => void>>();
  constructor(private readonly capturable = false) {}
  send(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    this.sent.push({ method, params });
    return Promise.resolve(method === "Page.captureScreenshot" ? { data: "SHOT" } : {});
  }
  on(event: string, handler: (p: any) => void): () => void {
    if (!this.handlers.has(event)) this.handlers.set(event, new Set());
    this.handlers.get(event)!.add(handler);
    return () => this.handlers.get(event)!.delete(handler);
  }
  fire(event: string, payload: any): void {
    for (const h of this.handlers.get(event) ?? []) h(payload);
  }
  goto(): Promise<void> { return Promise.resolve(); }
  url(): string { return "https://example.com"; }
  title(): Promise<string> { return Promise.resolve("Fake"); }
  close(): Promise<void> { return Promise.resolve(); }
  capture(): Promise<CaptureTarget | null> {
    return Promise.resolve(this.capturable ? { display: ":99", x: 0, y: 74, width: 800, height: 600 } : null);
  }
  countSent(method: string): number {
    return this.sent.filter((s) => s.method === method).length;
  }
}

function session(page: FakePage, webrtcAvailable: boolean): PanelSession {
  return new PanelSession({
    panelId: "pnl-rtc",
    page,
    viewport: { width: 800, height: 600 },
    now: () => 1,
    webrtcAvailable,
    webrtc: webrtcConfigFromEnv({}),
  });
}

describe("PanelSession — capability advertisement and automatic fallback", () => {
  it("advertises webrtc:false when the page has no capturable display", async () => {
    const page = new FakePage(false);
    const s = session(page, true);
    const seen: PanelMessage[] = [];
    s.attach((m) => seen.push(m));
    await s.start();
    expect(s.capabilities().webrtc).toBe(false);
    const ready = seen.find((m) => m.type === "ready") as { capabilities?: { webrtc: boolean } };
    expect(ready.capabilities).toMatchObject({ webrtc: false });
  });

  it("advertises webrtc:false when the POD lacks an encoder even if the page is capturable", async () => {
    const s = session(new FakePage(true), false);
    await s.start();
    expect(s.capabilities().webrtc).toBe(false);
  });

  it("advertises webrtc:true when page and pod can both do it", async () => {
    const s = session(new FakePage(true), true);
    await s.start();
    expect(s.capabilities()).toMatchObject({ webrtc: true, codecs: ["h264"] });
  });

  it("tells a client that asked for webrtc it is on screencast, and keeps sending frames", async () => {
    const page = new FakePage(false);
    const s = session(page, true);
    const seen: PanelMessage[] = [];
    const sub = s.attach((m) => seen.push(m));
    await s.start();
    await sub.signal({ type: "hello", webrtc: true });

    const transport = seen.find((m) => m.type === "transport") as { transport: string; reason: string };
    expect(transport).toMatchObject({ transport: "screencast" });
    expect(transport.reason).toContain("no capturable display");
    expect(sub.transport).toBe("screencast");

    // and the JPEG path is untouched — this is the non-negotiable bit.
    page.fire("Page.screencastFrame", { data: "AAAA", sessionId: 1 });
    expect(seen.filter((m) => m.type === "frame").length).toBeGreaterThan(0);
    expect(page.countSent("Page.stopScreencast")).toBe(0);
  });

  it("a v1 client that never says hello is never offered anything and keeps casting", async () => {
    const page = new FakePage(true);
    const s = session(page, true);
    const seen: PanelMessage[] = [];
    s.attach((m) => seen.push(m));
    await s.start();
    page.fire("Page.screencastFrame", { data: "AAAA", sessionId: 1 });
    expect(seen.some((m) => m.type === "offer")).toBe(false);
    expect(seen.some((m) => m.type === "transport")).toBe(false);
    expect(seen.some((m) => m.type === "frame")).toBe(true);
  });

  it("a hello with webrtc:false is answered with an explicit screencast verdict", async () => {
    const s = session(new FakePage(true), true);
    const seen: PanelMessage[] = [];
    const sub = s.attach((m) => seen.push(m));
    await s.start();
    await sub.signal({ type: "hello", webrtc: false });
    expect(seen.find((m) => m.type === "transport")).toMatchObject({ transport: "screencast", reason: "client did not request webrtc" });
  });

  it("reports byte cost per transport so the two can be compared", async () => {
    const page = new FakePage(false);
    const s = session(page, true);
    s.attach(() => {});
    await s.start();
    page.fire("Page.screencastFrame", { data: "A".repeat(1000), sessionId: 1 });
    const stats = s.stats();
    expect(stats.screencastBytes).toBeGreaterThanOrEqual(1000);
    expect(stats.screencastFrames).toBeGreaterThanOrEqual(1);
    expect(stats.videoBytes).toBe(0);
    expect(stats.transports).toEqual(["screencast"]);
  });
});
