/**
 * The panel pod's HTTP/WS face, end to end with the browser FAKED (no Chromium):
 * probes are open and name the mode, the demo client is served, POST
 * /panel/browser opens a panel + returns its wsPath, the WS streams ready +
 * screencast frames and dispatches inbound input into the (fake) page, nav +
 * DELETE work, capacity caps, and the introspection auth gate fails closed on
 * the data plane while probes stay open.
 */

import { describe, it, expect, afterEach } from "vitest";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import WebSocket from "ws";

import { startPanelServer } from "../../src/panel/server.js";
import type { BrowserDriver, PanelPage, OpenPageOptions } from "../../src/panel/browser.js";
import type { PanelIntrospector } from "../../src/panel/auth.js";

/** A fake CDP page whose screencast fires on demand via a test hook. */
class FakePage implements PanelPage {
  readonly sent: Array<{ method: string; params: Record<string, unknown> }> = [];
  private handlers = new Map<string, Set<(p: any) => void>>();
  private _url: string;
  constructor(url: string) {
    this._url = url;
  }
  send(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    this.sent.push({ method, params });
    // As soon as the screencast starts, push one frame so the WS test sees pixels.
    if (method === "Page.startScreencast") queueMicrotask(() => this.fire("Page.screencastFrame", { data: "IMGDATA", sessionId: 1, metadata: { deviceWidth: 800, deviceHeight: 600 } }));
    return Promise.resolve({});
  }
  on(event: string, handler: (p: any) => void): () => void {
    if (!this.handlers.has(event)) this.handlers.set(event, new Set());
    this.handlers.get(event)!.add(handler);
    return () => this.handlers.get(event)!.delete(handler);
  }
  fire(event: string, payload: any): void {
    for (const h of this.handlers.get(event) ?? []) h(payload);
  }
  goto(url: string): Promise<void> {
    this._url = url;
    return Promise.resolve();
  }
  url(): string {
    return this._url;
  }
  title(): Promise<string> {
    return Promise.resolve("T");
  }
  close(): Promise<void> {
    return Promise.resolve();
  }
}

class FakeDriver implements BrowserDriver {
  readonly pages: FakePage[] = [];
  open(_o: OpenPageOptions): Promise<PanelPage> {
    const p = new FakePage("https://example.com");
    this.pages.push(p);
    return Promise.resolve(p);
  }
  shutdown(): Promise<void> {
    return Promise.resolve();
  }
}

const servers: Server[] = [];
afterEach(async () => {
  for (const s of servers.splice(0)) await new Promise<void>((r) => s.close(() => r()));
});

async function boot(opts: { auth?: PanelIntrospector; serviceToken?: string; driver?: BrowserDriver; maxPanels?: number } = {}): Promise<{ base: string; driver: BrowserDriver }> {
  const driver = opts.driver ?? new FakeDriver();
  const server = startPanelServer(0, {
    env: {} as NodeJS.ProcessEnv, driver,
    ...(opts.auth ? { auth: opts.auth } : {}),
    ...(opts.serviceToken ? { serviceToken: opts.serviceToken } : {}),
    ...(opts.maxPanels ? { maxPanels: opts.maxPanels } : {}),
  });
  servers.push(server);
  await new Promise<void>((r) => server.once("listening", () => r()));
  return { base: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, driver };
}

async function openPanel(base: string, body: Record<string, unknown> = { url: "https://example.com" }): Promise<{ panelId: string; wsPath: string }> {
  const res = await fetch(`${base}/panel/browser`, { method: "POST", body: JSON.stringify(body) });
  expect(res.status).toBe(200);
  return (await res.json()) as { panelId: string; wsPath: string };
}

describe("panel pod — HTTP", () => {
  it("probes are open and name the mode; the demo client is served", async () => {
    const { base } = await boot();
    expect(await (await fetch(`${base}/healthz`)).json()).toEqual({ status: "ok", mode: "panel" });
    const v = (await (await fetch(`${base}/version`)).json()) as { mode: string; wire: string };
    expect(v.mode).toBe("panel");
    expect(v.wire).toBe("glyphh.panel/v2");
    const demo = await fetch(`${base}/panel/demo`);
    expect(demo.headers.get("content-type")).toMatch(/text\/html/);
    expect(await demo.text()).toContain("<canvas");
  });

  it("POST /panel/browser opens a panel and returns its wsPath", async () => {
    const { base, driver } = await boot();
    const { panelId, wsPath } = await openPanel(base);
    expect(panelId).toMatch(/^pnl-/);
    expect(wsPath).toBe(`/panel/browser/${panelId}`);
    expect((driver as FakeDriver).pages).toHaveLength(1);
  });

  it("rejects a bad url (400) and caps concurrent panels (409)", async () => {
    const { base } = await boot({ maxPanels: 1 });
    const bad = await fetch(`${base}/panel/browser`, { method: "POST", body: JSON.stringify({ url: "nope" }) });
    expect(bad.status).toBe(400);
    await openPanel(base);
    const busy = await fetch(`${base}/panel/browser`, { method: "POST", body: JSON.stringify({ url: "https://x.example" }) });
    expect(busy.status).toBe(409);
  });

  it("nav to a live panel works; nav/DELETE to an unknown panel 404", async () => {
    const { base } = await boot();
    const { panelId } = await openPanel(base);
    const nav = await fetch(`${base}/panel/browser/${panelId}/nav`, { method: "POST", body: JSON.stringify({ url: "https://other.example" }) });
    expect(nav.status).toBe(200);
    expect((await fetch(`${base}/panel/browser/pnl-ghost/nav`, { method: "POST", body: JSON.stringify({ url: "https://z" }) })).status).toBe(404);
    const del = await fetch(`${base}/panel/browser/${panelId}`, { method: "DELETE" });
    expect(del.status).toBe(200);
    // second delete → gone
    expect((await fetch(`${base}/panel/browser/${panelId}`, { method: "DELETE" })).status).toBe(404);
  });

  it("404s unknown routes", async () => {
    const { base } = await boot();
    expect((await fetch(`${base}/nope`)).status).toBe(404);
  });
});

describe("panel pod — WS stream", () => {
  it("greets with ready, streams a screencast frame, and dispatches input in", async () => {
    const { base, driver } = await boot();
    const { panelId, wsPath } = await openPanel(base);
    const msgs: Record<string, unknown>[] = [];
    await new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(`${base.replace("http", "ws")}${wsPath}`);
      ws.on("message", (data) => {
        const m = JSON.parse(data.toString()) as Record<string, unknown>;
        msgs.push(m);
        if (m.type === "ready") {
          // send an input event; it must reach the fake page as a CDP dispatch
          ws.send(JSON.stringify({ type: "mouse", action: "down", x: 5, y: 6, button: "left" }));
          // nudge a fresh frame by re-firing the screencast
          (driver as FakeDriver).pages[0].fire("Page.screencastFrame", { data: "IMGDATA", sessionId: 1, metadata: { deviceWidth: 800, deviceHeight: 600 } });
        }
        if (m.type === "frame") {
          // Let the async input dispatch settle before closing, then assert.
          setTimeout(() => {
            ws.close();
            resolve();
          }, 30);
        }
      });
      ws.on("error", reject);
    });
    expect(msgs[0]).toMatchObject({ type: "ready", panelId, wire: "glyphh.panel/v2" });
    expect(msgs.some((m) => m.type === "frame" && (m as { data: string }).data === "IMGDATA")).toBe(true);
    // the input reached the page
    const page = (driver as FakeDriver).pages[0];
    expect(page.sent.some((c) => c.method === "Input.dispatchMouseEvent" && c.params.type === "mousePressed")).toBe(true);
  });

  it("ping round-trips; a WS to an unknown panel is rejected", async () => {
    const { base } = await boot();
    const { wsPath } = await openPanel(base);
    const pong = await new Promise<boolean>((resolve, reject) => {
      const ws = new WebSocket(`${base.replace("http", "ws")}${wsPath}`);
      ws.on("message", (data) => {
        const m = JSON.parse(data.toString()) as { type: string };
        if (m.type === "ready") ws.send(JSON.stringify({ type: "ping" }));
        if (m.type === "pong") {
          ws.close();
          resolve(true);
        }
      });
      ws.on("error", reject);
    });
    expect(pong).toBe(true);
    await expect(
      new Promise((_, reject) => {
        const ws = new WebSocket(`${base.replace("http", "ws")}/panel/browser/pnl-ghost`);
        ws.on("error", reject);
      }),
    ).rejects.toThrow(/404/);
  });
});

describe("panel pod — panel-scoped auth (introspection)", () => {
  const SERVICE = "svc-secret";
  // A fake panel introspector: admits ONLY the token "good" and ONLY on the panel it
  // was minted for (`good` is bound to whatever panelId the pod minted, recorded here).
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

  it("fails closed on the data plane; probes + demo stay open", async () => {
    const { base } = await boot({ auth: denyAll, serviceToken: SERVICE });
    expect((await fetch(`${base}/healthz`)).status).toBe(200);
    expect((await fetch(`${base}/panel/demo`)).status).toBe(200);
    // Open without the service token → 401.
    expect((await fetch(`${base}/panel/browser`, { method: "POST", body: JSON.stringify({ url: "https://x" }) })).status).toBe(401);
    // WS with no/invalid token → 401 (denyAll).
    await expect(
      new Promise((_, reject) => {
        const ws = new WebSocket(`${base.replace("http", "ws")}/panel/browser/pnl-any`);
        ws.on("error", reject);
      }),
    ).rejects.toThrow(/401/);
  });

  it("opens with the SERVICE token, then admits the panel token ONLY on its own panel", async () => {
    let panelId: string | null = null;
    const { base } = await boot({ auth: boundIntrospector(() => panelId), serviceToken: SERVICE });

    // Open is service-gated: the control plane's service token opens the panel.
    const opened = await fetch(`${base}/panel/browser`, {
      method: "POST",
      headers: { authorization: `Bearer ${SERVICE}` },
      body: JSON.stringify({ url: "https://x.example" }),
    });
    expect(opened.status).toBe(200);
    panelId = ((await opened.json()) as { panelId: string }).panelId;

    // nav with the correct panel token → allowed.
    const navOk = await fetch(`${base}/panel/browser/${panelId}/nav`, {
      method: "POST",
      headers: { authorization: "Bearer good" },
      body: JSON.stringify({ url: "https://y.example" }),
    });
    expect(navOk.status).toBe(200);

    // nav with the SERVICE token → also allowed (the broker forwards nav/close on the
    // owner's behalf; only the browser's WS is restricted to the panel token).
    const navSvc = await fetch(`${base}/panel/browser/${panelId}/nav`, {
      method: "POST",
      headers: { authorization: `Bearer ${SERVICE}` },
      body: JSON.stringify({ url: "https://z.example" }),
    });
    expect(navSvc.status).toBe(200);

    // nav with a valid token but the WRONG panelId → 403 (token bound to another panel).
    const navWrong = await fetch(`${base}/panel/browser/pnl-someone-else/nav`, {
      method: "POST",
      headers: { authorization: "Bearer good" },
      body: JSON.stringify({ url: "https://y.example" }),
    });
    expect(navWrong.status).toBe(403);

    // WS with the correct panel token via query param → upgrades.
    await expect(
      new Promise<void>((resolve, reject) => {
        const ws = new WebSocket(`${base.replace("http", "ws")}/panel/browser/${panelId}?token=good`);
        ws.on("open", () => { ws.close(); resolve(); });
        ws.on("error", reject);
      }),
    ).resolves.toBeUndefined();

    // WS with a valid token but wrong panel → rejected (403).
    await expect(
      new Promise((_, reject) => {
        const ws = new WebSocket(`${base.replace("http", "ws")}/panel/browser/pnl-wrong?token=good`);
        ws.on("error", reject);
      }),
    ).rejects.toThrow(/403/);

    // WS with a bogus token → rejected (401).
    await expect(
      new Promise((_, reject) => {
        const ws = new WebSocket(`${base.replace("http", "ws")}/panel/browser/${panelId}?token=nope`);
        ws.on("error", reject);
      }),
    ).rejects.toThrow(/401/);

    // The WS does NOT accept the SERVICE token — it never reaches a browser, so the
    // stream is panel-token-only (the introspector treats "svc-secret" as a bad token).
    await expect(
      new Promise((_, reject) => {
        const ws = new WebSocket(`${base.replace("http", "ws")}/panel/browser/${panelId}?token=${SERVICE}`);
        ws.on("error", reject);
      }),
    ).rejects.toThrow(/401/);
  });
});
