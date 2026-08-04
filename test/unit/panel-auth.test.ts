/**
 * Panel-scoped auth unit tests. The panel introspector is the data-plane gate for a
 * MULTI-panel pod: it validates a caller's bearer against the control plane and binds
 * it to the SPECIFIC panel of the route, failing CLOSED on any doubt.
 *
 * `fetch` is mocked so no network is touched — every path (disabled, active+matching,
 * inactive, WRONG PANEL, missing bearer, transport failure, envelope shapes, per-panel
 * cache) is exercised deterministically.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { panelIntrospectorFromEnv } from "../../src/panel/auth.js";

function jsonResponse(status: number, body: unknown): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as unknown as Response;
}

const URL = "https://control.example/introspect";

function enabledEnv(over: Record<string, string> = {}): NodeJS.ProcessEnv {
  return { ROTOR_AUTH_INTROSPECT_URL: URL, ROTOR_AUTH_SERVICE_TOKEN: "svc-secret", ...over } as NodeJS.ProcessEnv;
}

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("panel introspector", () => {
  it("is DISABLED (pass-through) when no introspect URL is set", async () => {
    const auth = panelIntrospectorFromEnv({} as NodeJS.ProcessEnv);
    expect(auth.enabled).toBe(false);
    expect(await auth.authorizePanel(undefined, "pnl_a")).toEqual({ ok: true, status: 200 });
  });

  it("admits an active token bound to the requested panel", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { active: true, panelId: "pnl_a" }));
    const auth = panelIntrospectorFromEnv(enabledEnv());
    expect(await auth.authorizePanel("tok", "pnl_a")).toEqual({ ok: true, status: 200 });
  });

  it("rejects a VALID token minted for a DIFFERENT panel (403)", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { active: true, panelId: "pnl_a" }));
    const auth = panelIntrospectorFromEnv(enabledEnv());
    const d = await auth.authorizePanel("tok", "pnl_b");
    expect(d.ok).toBe(false);
    expect(d.status).toBe(403);
  });

  it("rejects an inactive token (401)", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { active: false }));
    const auth = panelIntrospectorFromEnv(enabledEnv());
    expect((await auth.authorizePanel("tok", "pnl_a")).status).toBe(401);
  });

  it("denies a missing bearer without calling the control plane", async () => {
    const auth = panelIntrospectorFromEnv(enabledEnv());
    expect((await auth.authorizePanel(undefined, "pnl_a")).status).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fails CLOSED on a transport error", async () => {
    fetchMock.mockRejectedValue(new Error("network down"));
    const auth = panelIntrospectorFromEnv(enabledEnv());
    expect((await auth.authorizePanel("tok", "pnl_a")).ok).toBe(false);
  });

  it("reads the payload under a `data` envelope too", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { data: { active: true, panelId: "pnl_a" } }));
    const auth = panelIntrospectorFromEnv(enabledEnv());
    expect((await auth.authorizePanel("tok", "pnl_a")).ok).toBe(true);
  });

  it("caches a positive decision per (panel, bearer) — a token cannot carry a cached allow onto another panel", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { active: true, panelId: "pnl_a" }));
    const auth = panelIntrospectorFromEnv(enabledEnv());
    await auth.authorizePanel("tok", "pnl_a"); // populates cache for (pnl_a, tok)
    await auth.authorizePanel("tok", "pnl_a"); // served from cache — no 2nd fetch
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // A different panel with the same bearer must re-introspect (and, here, 403).
    fetchMock.mockResolvedValue(jsonResponse(200, { active: true, panelId: "pnl_a" }));
    const d = await auth.authorizePanel("tok", "pnl_b");
    expect(d.status).toBe(403);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
