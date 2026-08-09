/**
 * auth/introspect.ts — OPTIONAL bearer-token introspection for an exposed worker.
 *
 * A rrotor worker serves exactly ONE session (SPEC.md §17.1 fungibility is about
 * pods behind the SAME session, not cross-session sharing). When a worker is
 * exposed on a network it must prove the caller holds a live runtime token BOUND
 * to this worker's session — otherwise anyone who reaches the pod runs rotors on
 * it. This module closes that gap by validating the caller's `Authorization:
 * Bearer <token>` against the control plane's introspection endpoint.
 *
 * It is OFF by default: with no `ROTOR_AUTH_INTROSPECT_URL` set, {@link
 * introspectorFromEnv} returns a pass-through whose `authorize` always allows —
 * so self-hosters and the existing test surface are unaffected. When the URL is
 * set, the data-plane routes (server.ts) fail CLOSED: any token we cannot verify
 * as active + session-matched is denied.
 *
 * The introspect contract (built server-side):
 *   POST ${ROTOR_AUTH_INTROSPECT_URL}
 *   Authorization: Bearer ${ROTOR_AUTH_SERVICE_TOKEN}
 *   content-type: application/json
 *   { "token": "<caller bearer>" }
 * Response is an envelope; the payload is parsed defensively (top level OR under
 * a `data` key): `{ active, sessionId, orgId?, userId?, status? }`.
 */

import { log } from "../obs/logger.js";

/** The principal a verified token resolves to — the control plane's introspect
 *  payload names the token's owner (`orgId`/`userId`). Thread persistence
 *  scopes every read and write to it (harness/threads.ts). */
export interface Principal {
  orgId: string;
  userId: string;
}

/** The outcome of an authorization check. `status` is the HTTP status the caller
 *  should receive on deny (401 = unauthenticated / unverifiable, 403 = verified
 *  but not bound to this session). `ok` true → allow the request. */
export interface AuthDecision {
  ok: boolean;
  status: number;
  reason?: string;
  /** The token's owner, when introspection supplied one (allow only). */
  principal?: Principal;
  /** The session the token is BOUND to, per introspection (allow only). On a
   *  shared pod (no ROTOR_SESSION_ID) the harness enforces per-run binding
   *  against this — a run's sessionId must be the token's. */
  sessionId?: string;
}

/** The auth seam the HTTP server threads through its data-plane routes. */
export interface Introspector {
  /** Whether enforcement is on. `false` → pass-through (no env URL configured). */
  readonly enabled: boolean;
  /** Validate a caller's bearer. Never throws — a transport failure denies (401). */
  authorize(bearer: string | undefined): Promise<AuthDecision>;
}

/** The introspection payload shape we care about (extra keys are ignored). */
interface IntrospectPayload {
  active?: unknown;
  sessionId?: unknown;
  orgId?: unknown;
  userId?: unknown;
}

/** Unwrap the envelope: the payload may be at the top level OR under `data`. */
function unwrap(body: unknown): IntrospectPayload {
  if (body && typeof body === "object") {
    const b = body as Record<string, unknown>;
    if (b.data && typeof b.data === "object") return b.data as IntrospectPayload;
    return b as IntrospectPayload;
  }
  return {};
}

/** A pass-through introspector: auth disabled, everything is allowed. */
const DISABLED: Introspector = {
  enabled: false,
  authorize: () => Promise.resolve({ ok: true, status: 200 }),
};

/** The disabled pass-through, for callers/tests that want an explicit default. */
export function disabledIntrospector(): Introspector {
  return DISABLED;
}

export interface IntrospectorConfig {
  /** The control-plane introspect endpoint (POST target). */
  url: string;
  /** The service token rrotor presents to the introspect endpoint. */
  serviceToken: string;
  /** The session id this worker serves (dedicated pod): the token's session
   *  MUST equal it. EMPTY = shared pod: any active token is admitted and the
   *  binding is enforced per run by the harness. */
  sessionId: string;
  /** Introspect request timeout, ms. Default 5000. */
  timeoutMs?: number;
  /** Positive-result cache TTL, ms. Default 30000. */
  cacheTtlMs?: number;
  /** Injectable clock (ms) — set in tests for deterministic cache expiry. */
  now?: () => number;
}

/** A cached positive decision, keyed by the caller's bearer, with an expiry. */
interface CacheEntry {
  decision: AuthDecision;
  expiresAt: number;
}

/**
 * The real introspector: it POSTs the caller's bearer to the control plane and
 * authorizes IFF the response is 2xx AND `active === true` AND `sessionId` equals
 * this worker's session. Everything else denies. Positive results are cached for
 * a short TTL so a stream of requests on one live token does not introspect on
 * every call; the service token is never cached anywhere it can leak.
 */
class HttpIntrospector implements Introspector {
  readonly enabled = true;
  private readonly cfg: Required<Omit<IntrospectorConfig, "now">> & { now: () => number };
  private readonly cache = new Map<string, CacheEntry>();

  constructor(cfg: IntrospectorConfig) {
    this.cfg = {
      url: cfg.url,
      serviceToken: cfg.serviceToken,
      sessionId: cfg.sessionId,
      timeoutMs: cfg.timeoutMs ?? 5000,
      cacheTtlMs: cfg.cacheTtlMs ?? 30000,
      now: cfg.now ?? Date.now,
    };
  }

  async authorize(bearer: string | undefined): Promise<AuthDecision> {
    if (!bearer) return { ok: false, status: 401, reason: "missing bearer" };

    const cached = this.cache.get(bearer);
    if (cached && cached.expiresAt > this.cfg.now()) return cached.decision;

    let payload: IntrospectPayload;
    try {
      payload = await this.introspect(bearer);
    } catch (err) {
      // Fail CLOSED: if we cannot verify the token we deny rather than admit.
      log.warn("introspect transport error", { detail: (err as Error).message });
      return { ok: false, status: 401 };
    }

    const active = payload.active === true;
    // TWO BINDING MODES. DEDICATED pod: provisioned with ROTOR_SESSION_ID —
    // only tokens bound to exactly that session are admitted (today's strict
    // check). SHARED pod: ROTOR_SESSION_ID unset — any ACTIVE token is
    // admitted, and the token's own sessionId rides the decision so the
    // harness enforces the binding PER RUN instead (POST /run rejects a body
    // sessionId that is not the token's — the same "session mismatch", moved
    // to where a shared pod can judge it).
    const matches = !this.cfg.sessionId || payload.sessionId === this.cfg.sessionId;
    if (active && matches) {
      // TWO response shapes from one endpoint: runtime/API-key credentials
      // introspect as {userId, orgId}; a USER SESSION token (gy_at_ — what the
      // control plane's turn proxy forwards) answers OAuth-style {sub, org}.
      // Accept both, or a front-door turn carries no principal and memory
      // silently never arms.
      const orgId = typeof payload.orgId === "string" ? payload.orgId
        : typeof (payload as { org?: unknown }).org === "string" ? (payload as { org: string }).org : "";
      const userId = typeof payload.userId === "string" ? payload.userId
        : typeof (payload as { sub?: unknown }).sub === "string" ? (payload as { sub: string }).sub : "";
      const sessionId = typeof payload.sessionId === "string" ? payload.sessionId : "";
      const decision: AuthDecision = {
        ok: true,
        status: 200,
        ...(sessionId ? { sessionId } : {}),
        ...(orgId && userId ? { principal: { orgId, userId } } : {}),
      };
      this.cache.set(bearer, { decision, expiresAt: this.cfg.now() + this.cfg.cacheTtlMs });
      return decision;
    }
    // Verified but inactive or bound to another session — deny, do not cache.
    return { ok: false, status: 403, reason: "session mismatch" };
  }

  /** POST the caller bearer to the introspect endpoint; return the parsed payload
   *  or throw (a thrown error denies, fail-closed, in {@link authorize}). */
  private async introspect(bearer: string): Promise<IntrospectPayload> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.cfg.timeoutMs);
    try {
      const res = await fetch(this.cfg.url, {
        method: "POST",
        headers: {
          authorization: `Bearer ${this.cfg.serviceToken}`,
          "content-type": "application/json",
        },
        body: JSON.stringify({ token: bearer }),
        signal: controller.signal,
      });
      if (!res.ok) throw new Error(`introspect returned ${res.status}`);
      return unwrap(await res.json());
    } finally {
      clearTimeout(timer);
    }
  }
}

/**
 * Build an introspector from the environment. When `ROTOR_AUTH_INTROSPECT_URL` is
 * UNSET, auth is DISABLED and a pass-through is returned (current behavior). When
 * set, enforcement is on: `ROTOR_AUTH_SERVICE_TOKEN` is the token rrotor presents
 * and `ROTOR_SESSION_ID` is the session this worker serves — leave it unset on a
 * SHARED pod (any active token admits; the harness binds sessions per run).
 */
export function introspectorFromEnv(env: NodeJS.ProcessEnv = process.env): Introspector {
  const url = env.ROTOR_AUTH_INTROSPECT_URL;
  if (!url) return DISABLED;
  return new HttpIntrospector({
    url,
    serviceToken: env.ROTOR_AUTH_SERVICE_TOKEN ?? "",
    sessionId: env.ROTOR_SESSION_ID ?? "",
  });
}

/** Extract the bearer token from an `Authorization: Bearer <token>` header value.
 *  Returns `undefined` when the header is absent or not a bearer credential. */
export function bearerFromHeader(header: string | string[] | undefined): string | undefined {
  const raw = Array.isArray(header) ? header[0] : header;
  if (typeof raw !== "string") return undefined;
  const match = /^Bearer\s+(.+)$/i.exec(raw.trim());
  return match ? match[1]!.trim() : undefined;
}
