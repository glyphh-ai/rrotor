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
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import WebSocket from "ws";

import { startPanelServer } from "../../src/panel/server.js";
import { webrtcConfigFromEnv } from "../../src/panel/webrtc.js";

const LIVE = process.env.PANEL_WEBRTC_LIVE === "1";

describe.skipIf(!LIVE)("panel pod — LIVE WebRTC negotiation + fallback", () => {
  let server: Server;

  afterAll(async () => {
    if (server) await new Promise<void>((r) => server.close(() => r()));
  });

  it("offers WebRTC, then falls back to the screencast when the client never answers", { timeout: 90_000 }, async () => {
    // A short negotiation window so the fallback assertion does not sit for the
    // production default.
    const webrtc = { ...webrtcConfigFromEnv(process.env), negotiationTimeoutMs: 4000 };
    server = startPanelServer(0, { env: process.env, webrtc });
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

    await new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(`${base.replace("http", "ws")}${wsPath}`);
      const deadline = setTimeout(() => { ws.close(); resolve(); }, 20_000);
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
          // The fallback verdict is the end of the test.
          if (m.transport === "screencast") {
            clearTimeout(deadline);
            ws.close();
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
      transports: string[]; screencastRunning: boolean; encoderRunning: boolean;
    };
    expect(stats.transports).toEqual(["screencast"]);
    expect(stats.screencastRunning).toBe(true);
    expect(stats.encoderRunning).toBe(false);

    const del = await fetch(`${base}/panel/browser/${panelId}`, { method: "DELETE" });
    expect(del.status).toBe(200);
  });
});
