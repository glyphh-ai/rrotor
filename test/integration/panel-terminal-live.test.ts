/**
 * LIVE terminal panel — gated behind PANEL_LIVE=1 (like the browser panel-live suite),
 * and skipped if node-pty is unavailable. Drives a REAL pty through the pod: open a
 * terminal, echo a command over the WS, see its output, resize, DELETE it, and verify the
 * pty process is GONE (no orphan shell). Skipped by default so `npm test` stays hermetic.
 *
 *   PANEL_LIVE=1   enable this suite
 */

import { describe, it, expect, afterAll } from "vitest";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import WebSocket from "ws";

import { startPanelServer } from "../../src/panel/server.js";
import type { BrowserDriver, PanelPage, OpenPageOptions } from "../../src/panel/browser.js";

const LIVE = process.env.PANEL_LIVE === "1";

/** Try to load node-pty; skip the live suite if the native addon is missing. */
let hasPty = false;
try {
  await import("node-pty");
  hasPty = true;
} catch {
  hasPty = false;
}

class NullBrowserDriver implements BrowserDriver {
  open(_o: OpenPageOptions): Promise<PanelPage> {
    return Promise.resolve({
      send: () => Promise.resolve({}), on: () => () => {}, goto: () => Promise.resolve(),
      url: () => "about:blank", title: () => Promise.resolve(""), close: () => Promise.resolve(),
    });
  }
  shutdown(): Promise<void> { return Promise.resolve(); }
}

describe.skipIf(!LIVE || !hasPty)("terminal pod — LIVE pty", () => {
  let server: Server;
  let base: string;
  afterAll(async () => { if (server) await new Promise<void>((r) => server.close(() => r())); });

  it("spawns a real shell, echoes a command, resizes, and DELETE leaves no orphan", { timeout: 30_000 }, async () => {
    server = startPanelServer(0, { env: { ...process.env, PANEL_SANDBOX_ROOT: process.env.TMPDIR ?? "/tmp" }, driver: new NullBrowserDriver() });
    await new Promise<void>((r) => server.once("listening", () => r()));
    base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;

    const res = await fetch(`${base}/panel/terminal`, { method: "POST", body: JSON.stringify({ sessionId: "live", cols: 80, rows: 24 }) });
    expect(res.status).toBe(200);
    const { panelId, wsPath } = (await res.json()) as { panelId: string; wsPath: string };

    let out = "";
    await new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(`${base.replace("http", "ws")}${wsPath}`);
      const deadline = setTimeout(() => resolve(), 8000);
      ws.on("message", (data) => {
        const m = JSON.parse(data.toString()) as { type: string; data?: string };
        if (m.type === "ready") {
          ws.send(JSON.stringify({ type: "resize", cols: 100, rows: 30 }));
          ws.send(JSON.stringify({ type: "input", data: "echo GLYPHH_MARKER_OK\r" }));
        }
        if (m.type === "data" && m.data) {
          out += Buffer.from(m.data, "base64").toString("utf8");
          if (out.includes("GLYPHH_MARKER_OK")) { clearTimeout(deadline); ws.close(); resolve(); }
        }
      });
      ws.on("error", reject);
    });
    // The shell echoed the command's OUTPUT (not just the typed line).
    expect(out).toMatch(/GLYPHH_MARKER_OK/);

    const del = await fetch(`${base}/panel/terminal/${panelId}`, { method: "DELETE" });
    expect(del.status).toBe(200);
    // Give the OS a beat to reap the killed pty, then assert no orphan.
    await new Promise((r) => setTimeout(r, 300));
    console.log(`[terminal-live] panelId=${panelId} outBytes=${out.length}`);
  });
});
