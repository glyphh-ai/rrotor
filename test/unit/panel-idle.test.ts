/**
 * The encoder IDLE GATE — the seam that makes a still page cost nothing on the
 * WebRTC path — tested pure (the state machine alone) and then wired through a
 * PanelSession with the encoder and peer FAKED, so every decision and every
 * side effect is asserted with no ffmpeg, no werift and no Chromium.
 *
 * The contract under test:
 *   • no motion for idleAfterMs → "pause", but ONLY after the encoder has
 *     proven media since its last start (an unproven encoder belongs to the
 *     negotiation-timeout story, not the gate);
 *   • a screencast frame while idle IS motion → "resume", and the parked
 *     subscribers keep receiving JPEG until the restarted encoder re-proves
 *     itself ("remute") — the client is never told "webrtc" ahead of packets;
 *   • the pause transition delivers a freeze-frame BEFORE the encoder dies, so
 *     a still page never goes black or stale (the bug class that bit us once);
 *   • probe-mode screencast frames are a motion signal only — never fanned
 *     out, never counted as wire bytes.
 */

import { describe, it, expect } from "vitest";

import { EncoderIdleGate } from "../../src/panel/idle.js";
import { PanelSession } from "../../src/panel/session.js";
import { PanelVideoSource, sourceRebaseHeader, webrtcConfigFromEnv } from "../../src/panel/webrtc.js";
import { viableCaptureTarget, MIN_CAPTURE_DIM } from "../../src/panel/encoder.js";
import type { PanelPage } from "../../src/panel/browser.js";
import type { CaptureTarget } from "../../src/panel/encoder.js";
import type { PanelMessage } from "../../src/panel/frames.js";

// ── the pure state machine ───────────────────────────────────────────────────

describe("EncoderIdleGate — decisions", () => {
  const gate = () => new EncoderIdleGate({ idleAfterMs: 1000, proofPackets: 3 });

  it("starts active and never pauses before the encoder has proven media", () => {
    const g = gate();
    expect(g.state).toBe("active");
    g.started(0);
    // An eternity of stillness, but zero packets — not the gate's call to make.
    expect(g.check(50_000)).toBeNull();
    expect(g.nextCheckAt()).toBeNull();
    expect(g.transitions).toBe(0);
  });

  it("remutes exactly once per activation, at the proof threshold", () => {
    const g = gate();
    g.started(0);
    expect(g.packet(10)).toBeNull();
    expect(g.packet(20)).toBeNull();
    expect(g.packet(30)).toBe("remute");
    expect(g.packet(40)).toBeNull();   // proof is edge-triggered, not level
    expect(g.nextCheckAt()).toBe(1000); // armed from the last motion (start)
  });

  it("pauses after a full stillness window, and motion pushes the deadline", () => {
    const g = gate();
    g.started(0);
    g.packet(1); g.packet(2); g.packet(3);
    expect(g.motion(500)).toBeNull();          // active motion is not a resume
    expect(g.nextCheckAt()).toBe(1500);
    expect(g.check(1499)).toBeNull();
    expect(g.check(1500)).toBe("pause");
    expect(g.state).toBe("idle");
    expect(g.transitions).toBe(1);
    expect(g.check(9999)).toBeNull();          // pause is edge-triggered too
    expect(g.nextCheckAt()).toBeNull();
  });

  it("ignores packets while idle — a stopping encoder's tail is not proof", () => {
    const g = gate();
    g.started(0);
    g.packet(1); g.packet(2); g.packet(3);
    g.check(1000);
    expect(g.state).toBe("idle");
    expect(g.packet(1001)).toBeNull();
    expect(g.packet(1002)).toBeNull();
    expect(g.state).toBe("idle");
  });

  it("resumes on motion while idle, demands FRESH proof, and measures the resume latency", () => {
    const g = gate();
    g.started(0);
    g.packet(1); g.packet(2); g.packet(3);
    expect(g.check(1000)).toBe("pause");
    expect(g.motion(2000)).toBe("resume");
    expect(g.state).toBe("active");
    expect(g.transitions).toBe(2);
    // Stale pre-pause proof does not count: three NEW packets are required.
    expect(g.packet(2120)).toBeNull();
    expect(g.lastResumeMs).toBe(120);          // motion→first packet
    expect(g.packet(2130)).toBeNull();
    expect(g.packet(2140)).toBe("remute");
    // …and the countdown re-arms for the next stillness.
    expect(g.nextCheckAt()).toBe(3000);
  });

  it("an encoder start while idle (a peer attaching) reactivates the gate", () => {
    const g = gate();
    g.started(0);
    g.packet(1); g.packet(2); g.packet(3);
    g.check(1000);
    expect(g.state).toBe("idle");
    g.started(5000);
    expect(g.state).toBe("active");
    expect(g.transitions).toBe(2);
    expect(g.nextCheckAt()).toBeNull();        // fresh start, fresh proof required
  });

  it("idleAfterMs <= 0 disables the gate completely", () => {
    const g = new EncoderIdleGate({ idleAfterMs: 0, proofPackets: 3 });
    g.started(0);
    g.packet(1); g.packet(2); g.packet(3);
    expect(g.check(1_000_000)).toBeNull();
    expect(g.motion(1_000_001)).toBeNull();
    expect(g.state).toBe("active");
    expect(g.nextCheckAt()).toBeNull();
  });
});

// ── the gate wired through a session (encoder + peer faked) ──────────────────

/** A fake CDP page with a capturable display. */
class FakePage implements PanelPage {
  readonly sent: Array<{ method: string; params: Record<string, unknown> }> = [];
  private handlers = new Map<string, Set<(p: any) => void>>();
  send(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    this.sent.push({ method, params });
    return Promise.resolve(method === "Page.captureScreenshot" ? { data: "FREEZE" } : {});
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
    return Promise.resolve({ display: ":99", x: 0, y: 0, width: 800, height: 600 });
  }
  lastCast(): Record<string, unknown> | undefined {
    return [...this.sent].reverse().find((c) => c.method === "Page.startScreencast")?.params;
  }
}

/** A fake PanelVideoSource: records pause/resume/restart, spawns nothing. */
class FakeSource {
  paused = 0;
  resumed = 0;
  restarted = 0;
  bytesOut = 0;
  running = false;
  restarts = 0;
  lastTarget: { width: number; height: number } | null = null;
  pause(): Promise<void> { this.paused++; this.running = false; return Promise.resolve(); }
  resume(): Promise<void> { this.resumed++; this.running = true; return Promise.resolve(); }
  restart(): Promise<void> { this.restarted++; this.restarts++; return Promise.resolve(); }
  stop(): Promise<void> { this.running = false; return Promise.resolve(); }
}

/** Harness: a live session on a fake page, its video source replaced by a fake,
 *  its clock hand-cranked, and one subscriber already "proven" on WebRTC. */
async function idleHarness(idleAfterMs = 1000) {
  const page = new FakePage();
  const clock = { t: 0 };
  const s = new PanelSession({
    panelId: "pnl-idle",
    page,
    viewport: { width: 800, height: 600 },
    now: () => clock.t,
    webrtcAvailable: true,
    webrtc: { ...webrtcConfigFromEnv({}), idleAfterMs },
  });
  const seen: PanelMessage[] = [];
  s.attach((m) => seen.push(m));
  await s.start();
  // Reach past the public surface: swap the real source (which would spawn
  // ffmpeg) for the fake, and put the subscriber straight into the proven-
  // WebRTC state a completed negotiation would have produced.
  const anyS = s as unknown as {
    source: FakeSource;
    subs: Set<{ transport: string; peer: unknown; idleParked: boolean }>;
    syncScreencast(): Promise<void>;
    onEncoderStarted(): void;
    onEncoderPacket(): void;
    runGateCheck(): void;
    encoderOps: Promise<void>;
  };
  const source = new FakeSource();
  anyS.source = source;
  const sub = [...anyS.subs][0];
  sub.transport = "webrtc";
  sub.peer = { close: () => Promise.resolve() };
  await anyS.syncScreencast();
  const prove = () => {
    anyS.onEncoderStarted();
    for (let i = 0; i < 8; i++) anyS.onEncoderPacket();
  };
  return { page, clock, s, anyS, source, sub, seen, prove };
}

describe("PanelSession — the idle gate end to end (fakes only)", () => {
  it("drops the cast to probe mode when every subscriber is on webrtc — it never stops", async () => {
    const { page, s } = await idleHarness();
    expect(s.stats().screencastRunning).toBe(true);
    expect(s.stats().screencastMode).toBe("probe");
    expect(page.lastCast()).toMatchObject({ maxWidth: 96, maxHeight: 96 });
    await s.close();
  });

  it("probe frames feed the gate but are never fanned out or counted", async () => {
    const { page, s, seen, prove } = await idleHarness();
    prove();
    const framesBefore = s.stats().screencastFrames;
    const seenBefore = seen.filter((m) => m.type === "frame").length;
    page.fire("Page.screencastFrame", { data: "PROBE", sessionId: 3 });
    expect(s.stats().screencastFrames).toBe(framesBefore);
    expect(seen.filter((m) => m.type === "frame").length).toBe(seenBefore);
    // …but the ACK is still mandatory.
    expect(page.sent.some((c) => c.method === "Page.screencastFrameAck" && c.params.sessionId === 3)).toBe(true);
    await s.close();
  });

  it("pauses after stillness: park on screencast, stop the encoder, freeze-frame from the reshaped cast", async () => {
    const { page, clock, s, anyS, source, sub, seen, prove } = await idleHarness();
    prove();
    clock.t = 2001;
    anyS.runGateCheck();
    await anyS.encoderOps;

    expect(source.paused).toBe(1);
    expect(sub.transport).toBe("screencast");
    expect(sub.idleParked).toBe(true);
    const idleAt = seen.findIndex((m) => m.type === "transport" && m.transport === "screencast" && /idle/.test(m.reason));
    expect(idleAt).toBeGreaterThanOrEqual(0);
    // Cast is back to full quality — it now carries the parked subscriber.
    expect(page.lastCast()).toMatchObject({ maxWidth: 800, quality: 80 });
    // The reshaped cast emits the current frame unprompted (Chromium behaviour;
    // the fake fires it here). It IS the freeze-frame: delivered to the parked
    // subscriber, inside the settle window, so it must NOT read as motion.
    clock.t = 2100;
    page.fire("Page.screencastFrame", { data: "SETTLE", sessionId: 5 });
    const frameAfter = seen.slice(idleAt + 1).find((m) => m.type === "frame") as { data: string } | undefined;
    expect(frameAfter?.data).toBe("SETTLE");
    await anyS.encoderOps;
    expect(source.resumed).toBe(0);
    expect(s.stats()).toMatchObject({ encoderGate: "idle", gateTransitions: 1, screencastMode: "full" });
    await s.close();
  });

  it("motion resumes the encoder while JPEG covers the gap; remute only after fresh proof", async () => {
    const { page, clock, s, anyS, source, sub, seen, prove } = await idleHarness();
    prove();
    clock.t = 2001;
    anyS.runGateCheck();
    await anyS.encoderOps;
    expect(s.stats().encoderGate).toBe("idle");

    // The reshaped (probe→full) cast emits settle repaints inside the settle
    // window. They are not motion — the encoder must stay paused or the pause
    // itself would flap the gate awake (the live-test-caught bug).
    clock.t = 2100;
    page.fire("Page.screencastFrame", { data: "REPAINT", sessionId: 8 });
    await anyS.encoderOps;
    expect(source.resumed).toBe(0);
    expect(s.stats().encoderGate).toBe("idle");

    // REAL motion: the next frame. It reaches the parked subscriber immediately
    // — that is the resume gap being covered.
    clock.t = 5000;
    const framesBefore = seen.filter((m) => m.type === "frame").length;
    page.fire("Page.screencastFrame", { data: "MOTION", sessionId: 9 });
    await anyS.encoderOps;
    expect(source.resumed).toBe(1);
    expect(seen.filter((m) => m.type === "frame").length).toBe(framesBefore + 1);
    expect(sub.transport).toBe("screencast");       // still parked: no proof yet

    // The respawned encoder proves itself → the subscriber is re-muted.
    clock.t = 5180;
    prove();
    await new Promise((r) => setTimeout(r, 0));  // let the fire-and-forget cast re-shape land
    expect(sub.transport).toBe("webrtc");
    expect(sub.idleParked).toBe(false);
    expect(seen.some((m) => m.type === "transport" && m.transport === "webrtc" && /resumed/.test(m.reason))).toBe(true);
    const st = s.stats();
    expect(st.encoderGate).toBe("active");
    expect(st.gateTransitions).toBe(2);
    expect(st.lastResumeMs).toBe(180);              // motion(5000) → first packet(5180)
    expect(st.screencastMode).toBe("probe");        // all-webrtc again
    await s.close();
  });

  it("a real peer fallback outranks parking — the subscriber is no longer remute material", async () => {
    const { clock, s, anyS, sub, prove } = await idleHarness();
    prove();
    clock.t = 2001;
    anyS.runGateCheck();
    await anyS.encoderOps;
    expect(sub.idleParked).toBe(true);
    // The peer dies while parked (session-level failure path).
    (anyS as unknown as { onVideoFailure(r: string): void }).onVideoFailure("encoder gone");
    expect(sub.idleParked).toBe(false);
    // A later remute must not resurrect it.
    clock.t = 3000;
    prove();
    expect(sub.transport).toBe("screencast");
    await s.close();
  });

  it("a resize while idle leaves the paused encoder alone — resume re-measures anyway", async () => {
    const { clock, s, anyS, source, prove } = await idleHarness();
    prove();
    clock.t = 2001;
    anyS.runGateCheck();
    await anyS.encoderOps;
    await s.dispatch({ type: "resize", width: 1000, height: 700 });
    expect(source.restarted).toBe(0);
    expect(s.stats().encoderGate).toBe("idle");
    await s.close();
  });

  it("PANEL_IDLE_AFTER_MS=0 disables the gate: stats say off, nothing ever pauses", async () => {
    const { s, anyS, source, prove } = await idleHarness(0);
    prove();
    anyS.runGateCheck();
    await anyS.encoderOps;
    expect(source.paused).toBe(0);
    expect(s.stats().encoderGate).toBe("off");
    await s.close();
  });

  it("a screencast-only panel reports the gate as off", async () => {
    const page = new FakePage();
    const s = new PanelSession({ panelId: "pnl-v1", page, viewport: { width: 800, height: 600 }, now: () => 1 });
    await s.start();
    expect(s.stats()).toMatchObject({ encoderGate: "off", gateTransitions: 0, lastResumeMs: null, screencastMode: "full" });
    await s.close();
  });
});

// ── resize-storm defence (the encoder must never chase an animation) ─────────

describe("PanelSession — resize settle: a burst of resizes is ONE encoder swap", () => {
  it("debounces the encoder restart to the trailing settle; the page/cast resize stays immediate", async () => {
    const { page, s, source, prove } = await idleHarness();
    prove();
    // A panel enter-animation: a resize per frame (observed live: 6 in 100ms).
    const castsBefore = page.sent.filter((c) => c.method === "Page.startScreencast").length;
    for (const [w, h] of [[500, 130], [500, 260], [500, 390], [700, 520], [1280, 800]] as const) {
      await s.dispatch({ type: "resize", width: w, height: h });
    }
    // The cheap paths tracked every step…
    expect(page.sent.filter((c) => c.method === "Page.startScreencast").length).toBe(castsBefore + 5);
    // …but the ffmpeg swap has not happened yet, and after the settle window it
    // happens exactly once.
    expect(source.restarted).toBe(0);
    await new Promise((r) => setTimeout(r, 420));
    expect(source.restarted).toBe(1);
    await s.close();
  });

  it("a resize burst against an IDLE panel never wakes the encoder", async () => {
    const { clock, s, anyS, source, prove } = await idleHarness();
    prove();
    clock.t = 2001;
    anyS.runGateCheck();
    await anyS.encoderOps;
    expect(s.stats().encoderGate).toBe("idle");
    await s.dispatch({ type: "resize", width: 1280, height: 800 });
    await new Promise((r) => setTimeout(r, 420));
    expect(source.restarted).toBe(0);
    expect(s.stats().encoderGate).toBe("idle");
    await s.close();
  });

  it("reports the thrash counters in stats", async () => {
    const { s, source } = await idleHarness();
    source.restarts = 3;
    source.lastTarget = { width: 1280, height: 800 };
    expect(s.stats()).toMatchObject({ encoderRestarts: 3, captureSize: "1280x800" });
    await s.close();
  });
});

describe("PanelVideoSource — degenerate capture rects", () => {
  const cfg = webrtcConfigFromEnv({});
  const sliver: CaptureTarget = { display: ":9", x: 0, y: 0, width: 500, height: 17 };

  it("viableCaptureTarget: anything under the floor in either dimension is not encodable", () => {
    expect(viableCaptureTarget({ display: ":9", x: 0, y: 0, width: 1280, height: 800 })).toBe(true);
    expect(viableCaptureTarget({ display: ":9", x: 0, y: 0, width: MIN_CAPTURE_DIM, height: MIN_CAPTURE_DIM })).toBe(true);
    expect(viableCaptureTarget(sliver)).toBe(false);
    expect(viableCaptureTarget({ display: ":9", x: 0, y: 0, width: 79, height: 600 })).toBe(false);
  });

  it("restart() against a sliver keeps the running encoder instead of swapping it for garbage", async () => {
    const failures: string[] = [];
    const src = new PanelVideoSource("pnl-sliver", cfg, () => Promise.resolve(sliver), (r) => failures.push(r));
    const prior = { stop: () => Promise.resolve(), bytesOut: 0 };
    const anySrc = src as unknown as { consumers: Set<(p: Buffer) => void>; encoder: unknown };
    anySrc.consumers.add(() => {});
    anySrc.encoder = prior;
    await src.restart();
    expect(anySrc.encoder).toBe(prior);   // untouched — no stop, no spawn
    expect(src.restarts).toBe(0);
    expect(failures).toEqual([]);         // a skip is not a failure
  });

  it("a FIRST start against a sliver is refused (screencast keeps the panel) — no ffmpeg is spawned", async () => {
    const src = new PanelVideoSource("pnl-sliver2", cfg, () => Promise.resolve(sliver), () => {});
    await expect(src.attach(() => {})).rejects.toThrow(/too small/);
    expect(src.running).toBe(false);
  });
});

// ── RTP continuity across encoder generations ────────────────────────────────

describe("sourceRebaseHeader — a restarted encoder must be seamless on the wire", () => {
  // werift's replaceRTP computes offsets as (lastSent − given): handing it
  // (firstSeq−1, firstTs−Δ) makes the new generation's first packet land at
  // exactly lastSent+1 / lastTs+Δ. Without this every restart froze the
  // browser's video (random seq jump → jitter buffer discards everything).
  it("backs off one sequence step and one frame interval", () => {
    expect(sourceRebaseHeader({ sequenceNumber: 100, timestamp: 90_000 }, 15))
      .toEqual({ sequenceNumber: 99, timestamp: 84_000 });
    expect(sourceRebaseHeader({ sequenceNumber: 100, timestamp: 90_000 }, 30))
      .toEqual({ sequenceNumber: 99, timestamp: 87_000 });
  });

  it("wraps both fields at their RTP boundaries", () => {
    expect(sourceRebaseHeader({ sequenceNumber: 0, timestamp: 100 }, 15))
      .toEqual({ sequenceNumber: 0xffff, timestamp: 0x1_0000_0000 - 5900 });
  });
});
