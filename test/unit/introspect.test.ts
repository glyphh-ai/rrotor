/**
 * Introspection auth unit tests. The introspector is the data-plane gate for an
 * exposed per-session worker: it validates a caller's bearer against the control
 * plane and binds it to THIS worker's session, failing CLOSED on any doubt.
 *
 * `fetch` is mocked so no network is touched — every path (disabled, active,
 * inactive, wrong session, missing bearer, transport failure, envelope shapes,
 * cache) is exercised deterministically.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { introspectorFromEnv, bearerFromHeader } from "../../src/auth/introspect.js";

/** Build a Response-like stub for the mocked fetch. */
function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

const URL = "https://control.example/introspect";
const SESSION = "sess-123";

/** The env that turns enforcement on. */
function enabledEnv(over: Record<string, string> = {}): NodeJS.ProcessEnv {
  return {
    ROTOR_AUTH_INTROSPECT_URL: URL,
    ROTOR_AUTH_SERVICE_TOKEN: "svc-secret",
    ROTOR_SESSION_ID: SESSION,
    ...over,
  } as NodeJS.ProcessEnv;
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("introspectorFromEnv — disabled (no URL)", () => {
  it("is a pass-through: authorize(undefined) is ok, no fetch", async () => {
    const auth = introspectorFromEnv({} as NodeJS.ProcessEnv);
    expect(auth.enabled).toBe(false);
    const decision = await auth.authorize(undefined);
    expect(decision).toEqual({ ok: true, status: 200 });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("introspectorFromEnv — enabled", () => {
  it("active + matching sessionId → ok, and sends the right request", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, { data: { active: true, sessionId: SESSION, status: "ready" } }),
    );
    const auth = introspectorFromEnv(enabledEnv());
    expect(auth.enabled).toBe(true);

    const decision = await auth.authorize("caller-token");
    expect(decision).toEqual({ ok: true, status: 200 });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe(URL);
    expect(init.method).toBe("POST");
    expect(init.headers.authorization).toBe("Bearer svc-secret");
    expect(init.headers["content-type"]).toBe("application/json");
    expect(JSON.parse(init.body)).toEqual({ token: "caller-token" });
    // A timeout signal is attached.
    expect(init.signal).toBeDefined();
  });

  it("inactive → denied 403", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { data: { active: false, sessionId: SESSION } }));
    const auth = introspectorFromEnv(enabledEnv());
    const decision = await auth.authorize("caller-token");
    expect(decision).toEqual({ ok: false, status: 403, reason: "session mismatch" });
  });

  it("wrong sessionId → denied 403", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { data: { active: true, sessionId: "other" } }));
    const auth = introspectorFromEnv(enabledEnv());
    const decision = await auth.authorize("caller-token");
    expect(decision).toEqual({ ok: false, status: 403, reason: "session mismatch" });
  });

  it("missing bearer → 401, no fetch", async () => {
    const auth = introspectorFromEnv(enabledEnv());
    const decision = await auth.authorize(undefined);
    expect(decision).toEqual({ ok: false, status: 401, reason: "missing bearer" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("introspect returns 500 → denied 401 (fail closed)", async () => {
    fetchMock.mockResolvedValue(jsonResponse(500, { error: "boom" }));
    const auth = introspectorFromEnv(enabledEnv());
    const decision = await auth.authorize("caller-token");
    expect(decision).toEqual({ ok: false, status: 401 });
  });

  it("introspect throws (transport error) → denied 401 (fail closed)", async () => {
    fetchMock.mockRejectedValue(new Error("econnrefused"));
    const auth = introspectorFromEnv(enabledEnv());
    const decision = await auth.authorize("caller-token");
    expect(decision).toEqual({ ok: false, status: 401 });
  });

  it("envelope: payload at the TOP level (no data key) works", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { active: true, sessionId: SESSION }));
    const auth = introspectorFromEnv(enabledEnv());
    const decision = await auth.authorize("caller-token");
    expect(decision.ok).toBe(true);
  });

  it("envelope: payload under a `data` key works", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { data: { active: true, sessionId: SESSION } }));
    const auth = introspectorFromEnv(enabledEnv());
    const decision = await auth.authorize("caller-token");
    expect(decision.ok).toBe(true);
  });

  it("caches a positive result: two authorize() within TTL do ONE fetch", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { data: { active: true, sessionId: SESSION } }));
    const auth = introspectorFromEnv(enabledEnv());

    const a = await auth.authorize("same-token");
    const b = await auth.authorize("same-token");
    expect(a.ok).toBe(true);
    expect(b.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("bearerFromHeader", () => {
  it("extracts the token from an Authorization: Bearer header", () => {
    expect(bearerFromHeader("Bearer abc.def")).toBe("abc.def");
    expect(bearerFromHeader("bearer  spaced ")).toBe("spaced");
    expect(bearerFromHeader(["Bearer first", "ignored"])).toBe("first");
  });

  it("returns undefined for absent or non-bearer headers", () => {
    expect(bearerFromHeader(undefined)).toBeUndefined();
    expect(bearerFromHeader("Basic xyz")).toBeUndefined();
    expect(bearerFromHeader("")).toBeUndefined();
  });
});
