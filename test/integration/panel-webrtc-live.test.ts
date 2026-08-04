/**
 * LIVE WebRTC panel — gated behind an env flag like the other panel-live suites.
 * Needs a REAL capturable display (Xvfb), a REAL ffmpeg and a REAL Chromium, so
 * it only runs where those exist (a Linux pod / the `panel` image), never in the
 * hermetic suite.
 *
 *   PANEL_WEBRTC_LIVE=1          enable this suite
 *   PANEL_CHROMIUM_PATH=<path>   (optional) pin the Chromium executable
 *
 * What it proves that the unit tests cannot: that a real pod actually OFFERS
 * WebRTC, and — the part that matters most in production — that a client which
 * asks for WebRTC and then goes silent (the corporate-firewall case: the offer
 * arrives, the UDP never does) is failed over to the JPEG screencast on a timer
 * and keeps receiving frames throughout. The fallback is the promise; this is
 * where it is checked against real Chromium rather than a fake.
 */

import { describe, it, expect, afterAll } from "vitest";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import WebSocket from "ws";

import { startPanelServer } from "../../src/panel/server.js";
import { webrtcConfigFromEnv } from "../../src/panel/webrtc.js";

const LIVE = process.env.PANEL_WEBRTC_LIVE === "1";

describe.skipIf(!LIVE)("panel pod — LIVE WebRTC negotiation + fallback", () => {
  let server: Server;
  const cleanup: Server[] = [];

  afterAll(async () => {
    for (const s of cleanup) await new Promise<void>((r) => s.close(() => r()));
  });

  it("offers WebRTC, then falls back to the screencast when the client never answers", { timeout: 90_000 }, async () => {
    // A short negotiation window so the fallback assertion does not sit for the
    // production default.
    const webrtc = { ...webrtcConfigFromEnv(process.env), negotiationTimeoutMs: 4000 };
    server = startPanelServer(0, { env: process.env, webrtc });
    cleanup.push(server);
    await new Promise<void>((r) => server.once("listening", () => r()));
    const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;

    const res = await fetch(`${base}/panel/browser`, {
      method: "POST",
      body: JSON.stringify({ url: "https://example.com", viewport: { width: 800, height: 600 } }),
    });
    expect(res.status).toBe(200);
    const { panelId, wsPath } = (await res.json()) as { panelId: string; wsPath: string };

    let sawOffer = false;
    let capabilities: { webrtc?: boolean } = {};
    const transports: Array<{ transport: string; reason: string }> = [];
    let framesAfterHello = 0;
    let helloSent = false;

    const ws = new WebSocket(`${base.replace("http", "ws")}${wsPath}`);
    await new Promise<void>((resolve, reject) => {
      const deadline = setTimeout(() => resolve(), 20_000);
      ws.on("message", (data) => {
        const m = JSON.parse(data.toString()) as Record<string, any>;
        if (m.type === "ready") {
          capabilities = m.capabilities ?? {};
          ws.send(JSON.stringify({ type: "hello", webrtc: true }));
          helloSent = true;
        }
        if (m.type === "offer") sawOffer = true;
        if (m.type === "frame" && helloSent) framesAfterHello++;
        if (m.type === "transport") {
          transports.push({ transport: m.transport, reason: m.reason });
          // The fallback verdict is the end of the test. The WS stays open until
          // the stats below are read — closing it would detach the subscriber
          // and empty the very transports list being asserted.
          if (m.transport === "screencast") {
            clearTimeout(deadline);
            resolve();
          }
        }
      });
      ws.on("error", reject);
    });

    // This pod HAS a display and an encoder — otherwise the suite is being run
    // somewhere it was not meant to be, and saying so is more useful than a
    // vacuously green test.
    expect(capabilities.webrtc).toBe(true);
    expect(sawOffer).toBe(true);
    expect(transports.at(-1)?.transport).toBe("screencast");
    expect(transports.at(-1)?.reason).toMatch(/timed out|failed/);
    // The whole point: the JPEG path never stopped.
    expect(framesAfterHello).toBeGreaterThan(0);

    const stats = await (await fetch(`${base}/panel/browser/${panelId}/stats`)).json() as {
      transports: string[]; screencastRunning: boolean; screencastMode: string; encoderRunning: boolean; encoderGate: string;
    };
    expect(stats.transports).toEqual(["screencast"]);
    expect(stats.screencastRunning).toBe(true);
    // With a screencast subscriber the cast is full-quality, and the gate —
    // which never saw a proven encoder — is still nominally active.
    expect(stats.screencastMode).toBe("full");
    expect(stats.encoderGate).toBe("active");
    expect(stats.encoderRunning).toBe(false);

    ws.close();
    const del = await fetch(`${base}/panel/browser/${panelId}`, { method: "DELETE" });
    expect(del.status).toBe(200);
  });

  it("idle-gates the encoder: still page → pause + JPEG unmute; motion → resume + re-mute", { timeout: 120_000 }, async () => {
    // A local still/motion pair so the cycle is deterministic: page A never
    // repaints after load; page B is one big repaint away.
    const pages = createServer((req, res) => {
      const still = (req.url ?? "/").includes("still");
      res.writeHead(200, { "content-type": "text/html" });
      res.end(`<!doctype html><body style="margin:0;background:${still ? "#fff" : "#000"};color:#111"><h1>${still ? "still" : "changed"}</h1></body>`);
    });
    await new Promise<void>((r) => pages.listen(0, "127.0.0.1", () => r()));
    cleanup.push(pages);
    const pagesPort = (pages.address() as AddressInfo).port;

    // A short stillness window so the pause is observed quickly.
    const webrtc = { ...webrtcConfigFromEnv(process.env), idleAfterMs: 1500 };
    server = startPanelServer(0, { env: process.env, webrtc });
    cleanup.push(server);
    await new Promise<void>((r) => server.once("listening", () => r()));
    const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;

    const res = await fetch(`${base}/panel/browser`, {
      method: "POST",
      body: JSON.stringify({ url: `http://127.0.0.1:${pagesPort}/still`, viewport: { width: 800, height: 600 } }),
    });
    expect(res.status).toBe(200);
    const { panelId, wsPath } = (await res.json()) as { panelId: string; wsPath: string };

    // A REAL client peer (werift as the answerer), so the pod's encoder truly
    // starts, truly proves media, and the gate has something to pause. The
    // client must speak the codec the pod offers or negotiation fails outright.
    const werift = await import("werift");
    const pc = new werift.RTCPeerConnection({
      codecs: {
        video: [new werift.RTCRtpCodecParameters({
          mimeType: "video/H264",
          clockRate: 90000,
          payloadType: 96,
          rtcpFeedback: [{ type: "nack" }, { type: "nack", parameter: "pli" }],
          parameters: "packetization-mode=1;level-asymmetry-allowed=1;profile-level-id=42e01f",
        })],
      },
    });
    let rtpPackets = 0;
    // Wire sequence numbers as RECEIVED by the peer — the continuity evidence.
    // A restarted encoder that is not re-based shows up here as a random jump
    // of thousands (which is what froze real browsers).
    const seqs: number[] = [];
    pc.onTrack.subscribe((track: { onReceiveRtp: { subscribe(fn: (rtp: { header: { sequenceNumber: number } }) => void): unknown } }) => {
      track.onReceiveRtp.subscribe((rtp) => {
        rtpPackets++;
        seqs.push(rtp.header.sequenceNumber);
      });
    });

    const transports: Array<{ transport: string; reason: string; at: number }> = [];
    let framesWhileParked = 0;
    const ws = new WebSocket(`${base.replace("http", "ws")}${wsPath}`);
    const waitFor = (pred: () => boolean, ms: number) =>
      new Promise<boolean>((resolve) => {
        const t0 = Date.now();
        const tick = (): void => {
          if (pred()) return resolve(true);
          if (Date.now() - t0 > ms) return resolve(false);
          setTimeout(tick, 100);
        };
        tick();
      });

    ws.on("message", (data) => {
      void (async () => {
        const m = JSON.parse(data.toString()) as Record<string, any>;
        if (m.type === "ready") ws.send(JSON.stringify({ type: "hello", webrtc: true }));
        else if (m.type === "offer") {
          await pc.setRemoteDescription({ type: "offer", sdp: m.sdp });
          const answer = await pc.createAnswer();
          await pc.setLocalDescription(answer);
          ws.send(JSON.stringify({ type: "answer", sdp: pc.localDescription?.sdp ?? answer.sdp }));
        } else if (m.type === "ice" && m.candidate) await pc.addIceCandidate(m.candidate).catch(() => {});
        else if (m.type === "transport") transports.push({ transport: m.transport, reason: m.reason, at: Date.now() });
        else if (m.type === "frame" && transports.at(-1)?.transport === "screencast" && /idle/.test(transports.at(-1)?.reason ?? "")) framesWhileParked++;
      })();
    });
    pc.onIceCandidate.subscribe((c: { candidate: string; sdpMid?: string; sdpMLineIndex?: number } | undefined) => {
      ws.send(JSON.stringify({ type: "ice", candidate: c ? { candidate: c.candidate, sdpMid: c.sdpMid ?? null, sdpMLineIndex: c.sdpMLineIndex ?? null } : null }));
    });

    const stats = async () => (await (await fetch(`${base}/panel/browser/${panelId}/stats`)).json()) as {
      encoderRunning: boolean; encoderGate: string; gateTransitions: number; lastResumeMs: number | null;
      screencastMode: string; videoBytes: number; encoderRestarts: number; captureSize: string | null;
      transports: string[];
    };

    const waitForStats = async (pred: (s: Awaited<ReturnType<typeof stats>>) => boolean, ms: number) => {
      const t0 = Date.now();
      for (;;) {
        const s = await stats();
        if (pred(s)) return s;
        if (Date.now() - t0 > ms) return null;
        await new Promise((r) => setTimeout(r, 200));
      }
    };

    // 1. WebRTC proves itself.
    expect(await waitFor(() => transports.some((t) => t.transport === "webrtc"), 20_000)).toBe(true);
    expect(rtpPackets).toBeGreaterThan(0);

    // 1b. RESIZE STORM: a sliver mid-animation plus the settled size, back to
    //     back. The pod must collapse this to ONE encoder swap at the settled
    //     geometry, and the peer must KEEP receiving media across the swap —
    //     measured at the receiving peer, not at ffmpeg.
    const packetsBeforeResize = rtpPackets;
    ws.send(JSON.stringify({ type: "resize", width: 500, height: 17 }));
    ws.send(JSON.stringify({ type: "resize", width: 900, height: 700 }));
    expect(await waitForStats((s) => s.encoderRestarts >= 1, 15_000)).toBeTruthy();
    const afterResize = await stats();
    expect(afterResize.encoderRestarts).toBe(1);          // one swap, not one per resize
    expect(afterResize.captureSize).toBe("900x700");      // at the settled size, never the sliver
    // The new generation actually reaches the peer.
    expect(await waitFor(() => rtpPackets > packetsBeforeResize + 8, 15_000)).toBe(true);

    // 2. The page is still → the gate pauses the encoder and parks the client
    //    back on the screencast, with the freeze-frame so nothing goes black.
    expect(await waitFor(() => transports.at(-1)?.transport === "screencast" && /idle/.test(transports.at(-1)?.reason ?? ""), 15_000)).toBe(true);
    const idle = await stats();
    expect(idle.encoderGate).toBe("idle");
    expect(idle.encoderRunning).toBe(false);
    expect(idle.screencastMode).toBe("full");
    // The freeze-frame (the reshaped cast's settle emission) lands moments
    // after the transport flip — the parked client is never left black.
    expect(await waitFor(() => framesWhileParked > 0, 10_000)).toBe(true);
    // …and while parked-and-still, the encoder emits NOTHING.
    const bytesAtPause = idle.videoBytes;
    await new Promise((r) => setTimeout(r, 2000));
    expect((await stats()).videoBytes).toBe(bytesAtPause);

    // 3. Motion (a navigation repaints the page) → resume, re-prove, re-mute.
    await fetch(`${base}/panel/browser/${panelId}/nav`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ url: `http://127.0.0.1:${pagesPort}/changed` }),
    });
    expect(await waitFor(() => transports.at(-1)?.transport === "webrtc" && /resumed/.test(transports.at(-1)?.reason ?? ""), 15_000)).toBe(true);
    const resumed = await stats();
    expect(resumed.encoderGate).toBe("active");
    expect(resumed.encoderRunning).toBe(true);
    expect(resumed.gateTransitions).toBeGreaterThanOrEqual(2);
    expect(resumed.lastResumeMs).not.toBeNull();
    expect(resumed.videoBytes).toBeGreaterThan(bytesAtPause);
    // …and the peer kept receiving across the pause→resume generation swap.
    const packetsAfterResume = rtpPackets;
    expect(await waitFor(() => rtpPackets > packetsAfterResume, 15_000)).toBe(true);

    // 4. CONTINUITY: over the whole run — initial generation, the resize swap,
    //    the idle resume swap — the received sequence numbers never jump.
    //    (Without re-basing, a swap is a random jump of thousands, which is
    //    exactly what froze real browsers.)
    expect(seqs.length).toBeGreaterThan(30);
    for (let i = 1; i < seqs.length; i++) {
      const delta = ((seqs[i] - seqs[i - 1] + 0x8000) % 0x10000) - 0x8000;
      expect(Math.abs(delta), `seq jump at packet ${i}: ${seqs[i - 1]} → ${seqs[i]}`).toBeLessThanOrEqual(100);
    }

    // 5. STALL-WATCHDOG DROP: the client kills its peer (4s no-frame watchdog)
    //    but keeps the socket. The pod must fall back to the screencast, send a
    //    keyframe (no black panel), and RELEASE the encoder — a dead peer must
    //    not pin an ffmpeg encoding for nobody.
    await pc.close();
    expect(await waitFor(() => transports.at(-1)?.transport === "screencast" && /failed|closed|disconnected/.test(transports.at(-1)?.reason ?? ""), 30_000)).toBe(true);
    expect(await waitForStats((s) => s.encoderRunning === false, 15_000)).toBeTruthy();
    const dropped = await stats();
    expect(dropped.transports).toEqual(["screencast"]);

    ws.close();
    await fetch(`${base}/panel/browser/${panelId}`, { method: "DELETE" });
    await new Promise<void>((r) => pages.close(() => r()));
  });
});
