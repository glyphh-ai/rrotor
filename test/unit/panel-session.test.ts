/**
 * The panel session + registry lifecycle, against a FAKE CDP page (no Chromium).
 * Asserts: screencast starts and each Page.screencastFrame becomes a `frame`
 * message + is ACKed; nav messages fire on url change; input events dispatch as
 * the right CDP command; resize re-emulates metrics and restarts the cast;
 * close() tears the context down and no message flows after; the registry
 * provisions/caps/closes panels and never leaks a page on a failed open.
 */

import { describe, it, expect, vi } from "vitest";

import { PanelSession, mintPanelId } from "../../src/panel/session.js";
import { PanelRegistry, PanelAtCapacity, BadPanelRequest } from "../../src/panel/registry.js";
import type { PanelPage, BrowserDriver, OpenPageOptions } from "../../src/panel/browser.js";
import type { PanelMessage } from "../../src/panel/frames.js";

/** A fake CDP page: records `send()` calls, lets a test fire CDP events, tracks url. */
class FakePage implements PanelPage {
  readonly sent: Array<{ method: string; params: Record<string, unknown> }> = [];
  closed = false;
  private handlers = new Map<string, Set<(p: any) => void>>();
  private _url: string;
  goToShouldFail = false;

  constructor(url = "about:blank") {
    this._url = url;
  }

  send(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    this.sent.push({ method, params });
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
  async goto(url: string): Promise<void> {
    if (this.goToShouldFail) throw new Error("nav blew up");
    this._url = url;
  }
  url(): string {
    return this._url;
  }
  title(): Promise<string> {
    return Promise.resolve("Fake Title");
  }
  close(): Promise<void> {
    this.closed = true;
    return Promise.resolve();
  }
  countSent(method: string): number {
    return this.sent.filter((s) => s.method === method).length;
  }
}

function makeSession(page: FakePage): PanelSession {
  return new PanelSession({ panelId: "pnl-test", page, viewport: { width: 800, height: 600 }, now: () => 111 });
}

describe("PanelSession — screencast + nav", () => {
  it("starts the screencast on start() and reports the initial nav", async () => {
    const page = new FakePage("https://example.com");
    const s = makeSession(page);
    const seen: PanelMessage[] = [];
    s.subscribe((m) => seen.push(m));
    await s.start();
    expect(s.status).toBe("live");
    expect(page.countSent("Page.startScreencast")).toBe(1);
    // subscribe() now fires a keyframe (Page.captureScreenshot) before start()'s
    // startScreencast, so find the screencast call by method rather than by index.
    const sc = page.sent.find((c) => c.method === "Page.startScreencast");
    expect(sc?.params).toMatchObject({ format: "jpeg", quality: 80, maxWidth: 800, maxHeight: 600 });
    // ready (on subscribe) then nav (on start).
    expect(seen[0]).toMatchObject({ type: "ready", panelId: "pnl-test", viewport: { width: 800, height: 600 } });
    expect(seen.find((m) => m.type === "nav")).toMatchObject({ type: "nav", url: "https://example.com", title: "Fake Title" });
  });

  it("turns each screencast frame into a `frame` message and ACKs it", async () => {
    const page = new FakePage("https://example.com");
    const s = makeSession(page);
    const seen: PanelMessage[] = [];
    s.subscribe((m) => seen.push(m));
    await s.start();
    page.fire("Page.screencastFrame", { data: "BASE64DATA", sessionId: 7, metadata: { deviceWidth: 800, deviceHeight: 600 } });
    const frame = seen.find((m) => m.type === "frame");
    expect(frame).toMatchObject({ type: "frame", data: "BASE64DATA", format: "jpeg", at: 111, meta: { deviceWidth: 800, deviceHeight: 600 } });
    // ACK is mandatory (Chromium stalls without it).
    expect(page.sent.some((c) => c.method === "Page.screencastFrameAck" && c.params.sessionId === 7)).toBe(true);
  });

  it("emits a nav only when the top-level url actually changes", async () => {
    const page = new FakePage("https://a.example");
    const s = makeSession(page);
    const navs: PanelMessage[] = [];
    s.subscribe((m) => m.type === "nav" && navs.push(m));
    await s.start();
    // same-url frameNavigated → no new nav
    page.fire("Page.frameNavigated", {});
    await Promise.resolve();
    // real change
    (page as unknown as { _url: string })._url = "https://b.example";
    page.fire("Page.frameNavigated", {});
    await Promise.resolve();
    await Promise.resolve();
    expect(navs.map((n) => (n as { url: string }).url)).toEqual(["https://a.example", "https://b.example"]);
  });
});

describe("PanelSession — input dispatch", () => {
  it("maps a mouse event to a CDP dispatch", async () => {
    const page = new FakePage("https://x");
    const s = makeSession(page);
    await s.start();
    await s.dispatch({ type: "mouse", action: "down", x: 3, y: 4, button: "left" });
    expect(page.sent.some((c) => c.method === "Input.dispatchMouseEvent" && c.params.type === "mousePressed")).toBe(true);
  });

  it("resize re-emulates device metrics and restarts the screencast", async () => {
    const page = new FakePage("https://x");
    const s = makeSession(page);
    await s.start();
    const before = page.countSent("Page.startScreencast");
    await s.dispatch({ type: "resize", width: 1000, height: 700 });
    expect(page.sent.some((c) => c.method === "Emulation.setDeviceMetricsOverride" && c.params.width === 1000)).toBe(true);
    expect(page.countSent("Page.stopScreencast")).toBe(1);
    expect(page.countSent("Page.startScreencast")).toBe(before + 1);
  });

  it("a no-op resize (same size) does nothing", async () => {
    const page = new FakePage("https://x");
    const s = makeSession(page);
    await s.start();
    const n = page.sent.length;
    await s.dispatch({ type: "resize", width: 800, height: 600 });
    expect(page.sent.length).toBe(n);
  });

  it("a dispatch failure emits an error, never throws", async () => {
    const page = new FakePage("https://x");
    const s = makeSession(page);
    const seen: PanelMessage[] = [];
    s.subscribe((m) => seen.push(m));
    await s.start();
    vi.spyOn(page, "send").mockRejectedValueOnce(new Error("cdp down"));
    await s.dispatch({ type: "mouse", action: "move", x: 1, y: 1 });
    expect(seen.some((m) => m.type === "error")).toBe(true);
  });
});

describe("PanelSession — teardown", () => {
  it("close() stops the cast, closes the page, and emits closed once", async () => {
    const page = new FakePage("https://x");
    const s = makeSession(page);
    const seen: PanelMessage[] = [];
    s.subscribe((m) => seen.push(m));
    await s.start();
    await s.close("bye");
    expect(page.countSent("Page.stopScreencast")).toBe(1);
    expect(page.closed).toBe(true);
    expect(seen.filter((m) => m.type === "closed")).toHaveLength(1);
    // no dispatch after close
    await s.dispatch({ type: "mouse", action: "move", x: 1, y: 1 });
    expect(page.sent.some((c) => c.method === "Input.dispatchMouseEvent")).toBe(false);
    // idempotent
    await s.close();
    expect(page.countSent("Page.stopScreencast")).toBe(1);
  });
});

describe("PanelRegistry", () => {
  const driver = (pageFactory: () => FakePage): BrowserDriver & { opened: FakePage[] } => {
    const opened: FakePage[] = [];
    return {
      opened,
      open: (_o: OpenPageOptions) => {
        const p = pageFactory();
        opened.push(p);
        return Promise.resolve(p);
      },
      shutdown: () => Promise.resolve(),
    };
  };

  it("provisions a panel: launches, navigates, starts the cast", async () => {
    const d = driver(() => new FakePage());
    const reg = new PanelRegistry(d);
    const { panelId, session } = await reg.open({ url: "https://example.com", viewport: { width: 640, height: 480 } });
    expect(panelId).toMatch(/^pnl-/);
    expect(reg.get(panelId)).toBe(session);
    expect(reg.count()).toBe(1);
    expect(d.opened[0].url()).toBe("https://example.com");
    expect(d.opened[0].countSent("Page.startScreencast")).toBe(1);
  });

  it("rejects a non-http url (400) and caps capacity (409)", async () => {
    const reg = new PanelRegistry(driver(() => new FakePage()), 1);
    await expect(reg.open({ url: "ftp://nope" })).rejects.toBeInstanceOf(BadPanelRequest);
    await reg.open({ url: "https://a" });
    await expect(reg.open({ url: "https://b" })).rejects.toBeInstanceOf(PanelAtCapacity);
  });

  it("does not register — and tears the page down — on a nav failure", async () => {
    const d = driver(() => {
      const p = new FakePage();
      p.goToShouldFail = true;
      return p;
    });
    const reg = new PanelRegistry(d);
    await expect(reg.open({ url: "https://boom" })).rejects.toThrow(/nav blew up/);
    expect(reg.count()).toBe(0);
    expect(d.opened[0].closed).toBe(true);
  });

  it("close() and closeAll() tear panels down; closeAll shuts the browser", async () => {
    const d = driver(() => new FakePage());
    const shutdown = vi.spyOn(d, "shutdown");
    const reg = new PanelRegistry(d, 4);
    const { panelId } = await reg.open({ url: "https://a" });
    await reg.open({ url: "https://b" });
    expect(await reg.close(panelId)).toBe(true);
    expect(await reg.close("pnl-ghost")).toBe(false);
    expect(reg.count()).toBe(1);
    await reg.closeAll();
    expect(reg.count()).toBe(0);
    expect(shutdown).toHaveBeenCalledOnce();
  });
});

describe("mintPanelId", () => {
  it("mints panel ids", () => {
    expect(mintPanelId()).toMatch(/^pnl-[a-z0-9]+-[a-z0-9]+$/);
  });
});
