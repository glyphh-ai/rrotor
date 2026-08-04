/**
 * LIVE browser panel — gated behind an env flag, like the pgvector-real suite:
 * set PANEL_LIVE=1 (and have playwright's Chromium available) to drive a REAL
 * headless browser through the pod: launch, navigate example.com, receive
 * screencast frames over WS, dispatch input, close cleanly. Skipped otherwise so
 * `npm test` stays hermetic (no Chromium download in CI unless asked).
 *
 *   PANEL_LIVE=1                 enable this suite
 *   PANEL_CHROMIUM_PATH=<path>   (optional) pin the Chromium executable
 */

import { describe, it, expect, afterAll } from "vitest";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import WebSocket from "ws";

import { startPanelServer } from "../../src/panel/server.js";

const LIVE = process.env.PANEL_LIVE === "1";

describe.skipIf(!LIVE)("panel pod — LIVE Chromium", () => {
  let server: Server;
  let base: string;

  afterAll(async () => {
    if (server) await new Promise<void>((r) => server.close(() => r()));
  });

  it("streams a real browser and takes input end to end", { timeout: 60_000 }, async () => {
    server = startPanelServer(0, { env: process.env });
    await new Promise<void>((r) => server.once("listening", () => r()));
    base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;

    const t0 = Date.now();
    const res = await fetch(`${base}/panel/browser`, {
      method: "POST",
      body: JSON.stringify({ url: "https://example.com", viewport: { width: 800, height: 600 } }),
    });
    expect(res.status).toBe(200);
    const { panelId, wsPath } = (await res.json()) as { panelId: string; wsPath: string };
    const openMs = Date.now() - t0;

    let frames = 0;
    let sawNav = false;
    let firstFrameMs = 0;
    await new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(`${base.replace("http", "ws")}${wsPath}`);
      const deadline = setTimeout(() => resolve(), 8000);
      ws.on("message", (data) => {
        const m = JSON.parse(data.toString()) as { type: string; url?: string };
        if (m.type === "ready") {
          // move the mouse + scroll to prove input flows without error
          ws.send(JSON.stringify({ type: "mouse", action: "move", x: 100, y: 100 }));
          ws.send(JSON.stringify({ type: "wheel", x: 100, y: 100, dx: 0, dy: 200 }));
        }
        if (m.type === "nav" && m.url?.includes("example.com")) sawNav = true;
        if (m.type === "frame") {
          if (!frames) firstFrameMs = Date.now() - t0;
          frames++;
          if (frames >= 1 && sawNav) {
            clearTimeout(deadline);
            ws.close();
            resolve();
          }
        }
      });
      ws.on("error", reject);
    });

    console.log(`[panel-live] openMs=${openMs} firstFrameMs=${firstFrameMs} frames=${frames} nav=${sawNav}`);
    expect(sawNav).toBe(true);
    expect(frames).toBeGreaterThanOrEqual(1);

    const del = await fetch(`${base}/panel/browser/${panelId}`, { method: "DELETE" });
    expect(del.status).toBe(200);
  });
});
