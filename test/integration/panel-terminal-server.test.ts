/**
 * The panel pod's TERMINAL HTTP/WS face, end to end with the pty FAKED (no node-pty):
 * POST /panel/terminal opens a terminal + returns its wsPath, the WS greets `ready` +
 * streams pty output as base64 `data` + feeds `input`/`resize` into the pty, DELETE kills
 * it, capacity caps, and the SAME panel-scoped introspection auth gates it (service token
 * opens; the panel token drives its own terminal WS; a bogus/wrong token is rejected).
 */

import { describe, it, expect, afterEach } from "vitest";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import WebSocket from "ws";

import { startPanelServer } from "../../src/panel/server.js";
import type { Pty, TerminalDriver, SpawnPtyOptions } from "../../src/panel/terminal-driver.js";
import type { BrowserDriver, PanelPage, OpenPageOptions } from "../../src/panel/browser.js";
import type { PanelIntrospector } from "../../src/panel/auth.js";

/** A fake pty whose output is a test hook; echoes writes back so an "echo" round-trips. */
class FakePty implements Pty {
  readonly pid = 999;
  killed = false;
  private dataCb: ((d: string) => void) | null = null;
  private exitCb: ((e: { exitCode: number }) => void) | null = null;
  write(data: string): void { queueMicrotask(() => this.dataCb?.(`echo:${data}`)); }
  resize(): void {}
  onData(cb: (d: string) => void): void { this.dataCb = cb; }
  onExit(cb: (e: { exitCode: number }) => void): void { this.exitCb = cb; }
  kill(): void { this.killed = true; this.exitCb?.({ exitCode: 0 }); }
}
class FakeTermDriver implements TerminalDriver {
  readonly ptys: FakePty[] = [];
  spawn(_o: SpawnPtyOptions): Promise<Pty> { const p = new FakePty(); this.ptys.push(p); return Promise.resolve(p); }
  shutdown(): Promise<void> { return Promise.resolve(); }
}

/** A no-op browser driver so the pod boots without Chromium (we only test terminals). */
class NullBrowserDriver implements BrowserDriver {
  open(_o: OpenPageOptions): Promise<PanelPage> {
    return Promise.resolve({
      send: () => Promise.resolve({}), on: () => () => {}, goto: () => Promise.resolve(),
      url: () => "about:blank", title: () => Promise.resolve(""), close: () => Promise.resolve(),
    });
  }
  shutdown(): Promise<void> { return Promise.resolve(); }
}

const servers: Server[] = [];
afterEach(async () => {
  for (const s of servers.splice(0)) await new Promise<void>((r) => s.close(() => r()));
});

async function boot(opts: { auth?: PanelIntrospector; serviceToken?: string; maxTerminals?: number } = {}): Promise<{ base: string; term: FakeTermDriver }> {
  const term = new FakeTermDriver();
  const server = startPanelServer(0, {
    env: { PANEL_SANDBOX_ROOT: process.env.TMPDIR ?? "/tmp" } as NodeJS.ProcessEnv,
    driver: new NullBrowserDriver(), terminalDriver: term,
    ...(opts.auth ? { auth: opts.auth } : {}),
    ...(opts.serviceToken ? { serviceToken: opts.serviceToken } : {}),
    ...(opts.maxTerminals ? { maxTerminals: opts.maxTerminals } : {}),
  });
  servers.push(server);
  await new Promise<void>((r) => server.once("listening", () => r()));
  return { base: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, term };
}

async function openTerm(base: string, body: Record<string, unknown> = { sessionId: "s", cols: 80, rows: 24 }): Promise<{ panelId: string; wsPath: string; wire: string }> {
  const res = await fetch(`${base}/panel/terminal`, { method: "POST", body: JSON.stringify(body) });
  expect(res.status).toBe(200);
  return (await res.json()) as { panelId: string; wsPath: string; wire: string };
}

describe("terminal pod — HTTP", () => {
  it("POST /panel/terminal opens a terminal and returns its wsPath + terminal wire", async () => {
    const { base, term } = await boot();
    const { panelId, wsPath, wire } = await openTerm(base);
    expect(panelId).toMatch(/^trm-/);
    expect(wsPath).toBe(`/panel/terminal/${panelId}`);
    expect(wire).toBe("glyphh.terminal/v1");
    expect(term.ptys).toHaveLength(1);
  });

  it("caps concurrent terminals (409) and DELETE kills the pty (404 when unknown)", async () => {
    const { base, term } = await boot({ maxTerminals: 1 });
    const { panelId } = await openTerm(base);
    const busy = await fetch(`${base}/panel/terminal`, { method: "POST", body: JSON.stringify({ sessionId: "s" }) });
    expect(busy.status).toBe(409);
    const del = await fetch(`${base}/panel/terminal/${panelId}`, { method: "DELETE" });
    expect(del.status).toBe(200);
    expect(term.ptys[0].killed).toBe(true); // pty killed → no orphan shell
    expect((await fetch(`${base}/panel/terminal/${panelId}`, { method: "DELETE" })).status).toBe(404);
  });
});

describe("terminal pod — WS stream", () => {
  it("greets ready, echoes an input command back as pty output, resizes", async () => {
    const { base } = await boot();
    const { panelId, wsPath } = await openTerm(base);
    const msgs: Record<string, unknown>[] = [];
    await new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(`${base.replace("http", "ws")}${wsPath}`);
      ws.on("message", (data) => {
        const m = JSON.parse(data.toString()) as Record<string, unknown>;
        msgs.push(m);
        if (m.type === "ready") {
          ws.send(JSON.stringify({ type: "resize", cols: 120, rows: 40 }));
          ws.send(JSON.stringify({ type: "input", data: "whoami\r" }));
        }
        if (m.type === "data") { setTimeout(() => { ws.close(); resolve(); }, 20); }
      });
      ws.on("error", reject);
    });
    expect(msgs[0]).toMatchObject({ type: "ready", panelId, wire: "glyphh.terminal/v1" });
    const data = msgs.find((m) => m.type === "data") as { data: string };
    // The fake pty echoes writes; the client sees "echo:whoami\r".
    expect(Buffer.from(data.data, "base64").toString("utf8")).toBe("echo:whoami\r");
  });

  it("ping round-trips; a WS to an unknown terminal is rejected", async () => {
    const { base } = await boot();
    const { wsPath } = await openTerm(base);
    const pong = await new Promise<boolean>((resolve, reject) => {
      const ws = new WebSocket(`${base.replace("http", "ws")}${wsPath}`);
      ws.on("message", (data) => {
        const m = JSON.parse(data.toString()) as { type: string };
        if (m.type === "ready") ws.send(JSON.stringify({ type: "ping" }));
        if (m.type === "pong") { ws.close(); resolve(true); }
      });
      ws.on("error", reject);
    });
    expect(pong).toBe(true);
    await expect(
      new Promise((_, reject) => {
        const ws = new WebSocket(`${base.replace("http", "ws")}/panel/terminal/trm-ghost`);
        ws.on("error", reject);
      }),
    ).rejects.toThrow(/404/);
  });
});

describe("terminal pod — panel-scoped auth (introspection)", () => {
  const SERVICE = "svc-secret";
  function boundIntrospector(boundPanelId: () => string | null): PanelIntrospector {
    return {
      enabled: true,
      authorizePanel: (bearer, panelId) => {
        if (bearer !== "good") return Promise.resolve({ ok: false, status: 401, reason: "bad token" });
        if (panelId !== boundPanelId()) return Promise.resolve({ ok: false, status: 403, reason: "panel mismatch" });
        return Promise.resolve({ ok: true, status: 200 });
      },
    };
  }
  const denyAll: PanelIntrospector = { enabled: true, authorizePanel: () => Promise.resolve({ ok: false, status: 401, reason: "no token" }) };

  it("open is service-gated; the WS fails closed without the panel token", async () => {
    const { base } = await boot({ auth: denyAll, serviceToken: SERVICE });
    // Open without the service token → 401.
    expect((await fetch(`${base}/panel/terminal`, { method: "POST", body: JSON.stringify({ sessionId: "s" }) })).status).toBe(401);
    // WS with no/invalid token → 401 (denyAll).
    await expect(
      new Promise((_, reject) => {
        const ws = new WebSocket(`${base.replace("http", "ws")}/panel/terminal/trm-any`);
        ws.on("error", reject);
      }),
    ).rejects.toThrow(/401/);
  });

  it("opens with the SERVICE token, then admits the panel token ONLY on its own terminal", async () => {
    let panelId: string | null = null;
    const { base } = await boot({ auth: boundIntrospector(() => panelId), serviceToken: SERVICE });
    const opened = await fetch(`${base}/panel/terminal`, {
      method: "POST", headers: { authorization: `Bearer ${SERVICE}` }, body: JSON.stringify({ sessionId: "s" }),
    });
    expect(opened.status).toBe(200);
    panelId = ((await opened.json()) as { panelId: string }).panelId;

    // WS with the correct panel token via query param → upgrades.
    await expect(
      new Promise<void>((resolve, reject) => {
        const ws = new WebSocket(`${base.replace("http", "ws")}/panel/terminal/${panelId}?token=good`);
        ws.on("open", () => { ws.close(); resolve(); });
        ws.on("error", reject);
      }),
    ).resolves.toBeUndefined();

    // WS with a valid token but wrong terminal → 403; a bogus token → 401.
    await expect(new Promise((_, reject) => { new WebSocket(`${base.replace("http", "ws")}/panel/terminal/trm-wrong?token=good`).on("error", reject); })).rejects.toThrow(/403/);
    await expect(new Promise((_, reject) => { new WebSocket(`${base.replace("http", "ws")}/panel/terminal/${panelId}?token=nope`).on("error", reject); })).rejects.toThrow(/401/);
  });
});
