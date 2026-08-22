/**
 * service.ts — the app-worker COMPOSITION ROOT for a pod (workpanel apps,
 * slice 5a): the one object that turns the prior slices into a serving unit.
 *
 * Given { controlPlaneUrl, runtimeToken, cacheDir } it lazily arms one app per
 * (org, slug) on first use:
 *
 *   1. MINT the app's `gy_wk_` WORKER SERVICE TOKEN via the SDK
 *      (`apps.mintWorkerToken`, authenticated by the pod's own `gy_rk_` runtime
 *      token — the control-plane credential the pod already holds). Mint and
 *      rotate are the same server call: one live handle per (org, app), so the
 *      minted token is cached for the life of this process and the previous
 *      pod's handle dies the moment this one exists (the spec's one-execution-
 *      home rule — a slug moves BETWEEN pods, never onto two at once).
 *   2. RESOLVE the bundle through the content-addressed {@link AppBundleCache}
 *      (presigned R2 download, sha-256 verified, re-fetch of an unchanged
 *      release is a no-op).
 *   3. BUILD the app's capability bridge (capability-bridge.ts) around the
 *      minted worker token and register it with the shared executor, so the
 *      worker script's `glyphh.call` reaches the app's data planes and its
 *      declared org connectors — and nothing else.
 *
 * `invoke(orgId, slug, handler, args)` then runs one worker handler through
 * the sandboxed {@link AppWorkerExecutor}. The org is a BINDING, not a router:
 * the pod's runtime token belongs to exactly one org, so a caller org that is
 * not the minted token's org is refused — this pod simply is not that org's
 * execution home.
 *
 * One {@link AppCronService} rides the same composition: schedules an app's
 * worker registers via `glyphh.call("cron.schedule", …)` persist in the
 * {@link CronStore} (Postgres on a stator-configured pod — the SAME DSN the
 * thread store uses, see {@link appWorkerServiceFromEnv}; memory otherwise)
 * and {@link AppWorkerService.start} re-arms them at boot. A firing goes back
 * through {@link AppWorkerService.invoke}'s ensure path, so a schedule for an
 * app this process has not touched yet re-mints and re-materializes on demand.
 *
 * Failures crossing the HTTP surface are TYPED ({@link classifyWorkerError}):
 * timeout / capability-denied / not-installed / forbidden / no-handler /
 * invoke-error — each with the HTTP status server.ts should answer.
 */

import { join } from "node:path";
import { tmpdir } from "node:os";

import { createClient, ApiError } from "@glyphh/sdk";
import type { Client } from "@glyphh/sdk";

import { AppBundleCache } from "./bundle.js";
import type { ResolvedApp } from "./bundle.js";
import { AppWorkerExecutor, INVOKE_TIMEOUT_MS } from "./executor.js";
import { createCapabilityBridge, executorBridge } from "./capability-bridge.js";
import type { AppCapabilityBridge } from "./capability-bridge.js";
import { AppCronService, MemoryCronStore, PgCronStore } from "./cron.js";
import type { CronStore } from "./cron.js";
import { statorConfigured } from "../harness/threads.js";
import { log } from "../obs/logger.js";
import type { CapabilityStatus } from "../runtime/registry.js";

// ── typed failures ──────────────────────────────────────────────────────────

/** The error kinds the invoke surface reports. The first three are the route's
 *  contract (timeout, capability-denied, not-installed); the rest keep the
 *  remaining failure shapes just as precise. */
export type AppWorkerErrorKind =
  | "timeout"
  | "capability-denied"
  | "not-installed"
  | "forbidden"
  | "no-handler"
  | "invoke-error";

/** A classified worker failure: kind + the HTTP status the route answers. */
export class AppWorkerError extends Error {
  constructor(
    readonly kind: AppWorkerErrorKind,
    message: string,
    readonly httpStatus: number,
  ) {
    super(message);
    this.name = "AppWorkerError";
  }
}

/**
 * Classify any failure from the mint/resolve/invoke pipeline into ONE typed
 * shape. Pattern-matched on the precise messages the underlying modules emit
 * (executor timeouts, bridge grant refusals, the server's install gate) — the
 * same strings their unit tests pin down.
 */
export function classifyWorkerError(err: unknown): AppWorkerError {
  if (err instanceof AppWorkerError) return err;
  const message = err instanceof Error ? err.message : String(err);

  // The control plane's own refusals (SDK errors from mint / bundle / bridge).
  if (err instanceof ApiError) {
    if (/not installed/i.test(message) || err.httpStatus === 404) {
      return new AppWorkerError("not-installed", message, 404);
    }
    if (err.httpStatus === 401 || err.httpStatus === 403) {
      return new AppWorkerError("forbidden", message, 403);
    }
    return new AppWorkerError("invoke-error", message, 502);
  }

  if (/timed out after \d+ms/.test(message)) {
    return new AppWorkerError("timeout", message, 504);
  }
  if (/capability not granted:|is not available in the pod runtime|unknown capability:|connector not declared in manifest/.test(message)) {
    return new AppWorkerError("capability-denied", message, 403);
  }
  if (/not installed/i.test(message)) {
    return new AppWorkerError("not-installed", message, 404);
  }
  if (/no handler registered:|declares no worker script/.test(message)) {
    return new AppWorkerError("no-handler", message, 404);
  }
  return new AppWorkerError("invoke-error", message, 500);
}

/**
 * Did the control plane refuse the app's MINTED worker token on a data call?
 * Two shapes reach the arm's guard: a raw SDK {@link ApiError} (401), or the
 * capability bridge's `surfaced` wrapper — a plain Error carrying
 * `refused by the control plane (HTTP 401 …)` (capability-bridge.ts), the
 * exact string its unit tests pin down.
 */
export function isWorkerTokenRejection(err: unknown): boolean {
  if (err instanceof ApiError) return err.httpStatus === 401;
  const message = err instanceof Error ? err.message : String(err);
  return /refused by the control plane \(HTTP 401 /.test(message);
}

// ── the service ─────────────────────────────────────────────────────────────

export interface AppWorkerServiceOptions {
  /** Control-plane base URL, e.g. https://api.glyphh.ai. */
  controlPlaneUrl: string;
  /** The pod's `gy_rk_` runtime token — the credential that mints worker
   *  tokens and downloads bundles. Held like a secret, never logged. */
  runtimeToken: string;
  /** Root for materialized bundles (`<cacheDir>/<sha256>/…`). */
  cacheDir: string;
  /** Durable schedule rows. Absent → cron.* capabilities answer unsupported. */
  cronStore?: CronStore;
  /** Injection seam for tests; defaults to global fetch. */
  fetchImpl?: typeof fetch;
  /** Handler invoke cap per call (default: the executor's 60s). */
  invokeTimeoutMs?: number;
  /** Worker readiness cap override for tests. */
  readyTimeoutMs?: number;
}

/** One armed app: the org its minted worker token belongs to. */
interface AppEntry {
  orgId: string;
  slug: string;
}

export class AppWorkerService {
  /** The control-plane client authenticated as THE POD (`gy_rk_`) — mints only. */
  private readonly control: Client;
  private readonly bundles: AppBundleCache;
  /** slug → its worker-token bridge; the executor routes `glyphh.call` here. */
  private readonly bridges = new Map<string, AppCapabilityBridge>();
  private readonly executor: AppWorkerExecutor;
  /** slug → armed entry, coalesced: concurrent first invokes mint ONCE. */
  private readonly entries = new Map<string, Promise<AppEntry>>();
  readonly cron: AppCronService | undefined;
  private readonly cronStore: CronStore | undefined;

  constructor(private readonly opts: AppWorkerServiceOptions) {
    this.control = createClient({
      url: opts.controlPlaneUrl,
      apiKey: opts.runtimeToken,
      ...(opts.fetchImpl ? { fetch: opts.fetchImpl } : {}),
    });
    this.bundles = new AppBundleCache({
      cacheDir: opts.cacheDir,
      controlPlaneUrl: opts.controlPlaneUrl,
      token: opts.runtimeToken,
      ...(opts.fetchImpl ? { fetchImpl: opts.fetchImpl } : {}),
    });
    this.executor = new AppWorkerExecutor({
      // Every invoke re-resolves through the content-addressed cache (a cheap
      // control-plane GET when the hash is unchanged), AFTER the entry is armed
      // — so a worker never starts without its capability bridge in place.
      resolveApp: (slug) => this.resolveArmed(slug),
      capabilityBridge: executorBridge(this.bridges),
      ...(opts.readyTimeoutMs !== undefined ? { readyTimeoutMs: opts.readyTimeoutMs } : {}),
    });
    if (opts.cronStore) {
      this.cronStore = opts.cronStore;
      this.cron = new AppCronService({
        // Fire through the ENSURE path, not the raw executor: a persisted
        // schedule must re-arm an app this process has never invoked.
        executor: {
          invokeHandler: (slug, name, args, timeoutMs) => this.invoke("", slug, name, args, timeoutMs),
        },
        store: opts.cronStore,
        ...(opts.invokeTimeoutMs !== undefined ? { invokeTimeoutMs: opts.invokeTimeoutMs } : {}),
      });
    }
  }

  /** Boot: re-arm every persisted cron schedule (rows that no longer validate
   *  are dropped, not fatal — the cron service's desktop-parity rule). */
  async start(): Promise<void> {
    await this.cron?.start();
  }

  /**
   * Run one worker handler. `orgId` (the introspected principal's org) BINDS:
   * empty string skips the check (internal cron firings — the schedule rows are
   * already scoped by the token that created them); a non-empty org must be the
   * minted worker token's org or the call is refused. All failures are thrown
   * as {@link AppWorkerError}.
   */
  async invoke(orgId: string, slug: string, handler: string, args: unknown = {}, timeoutMs?: number): Promise<unknown> {
    try {
      const entry = await this.entry(slug);
      if (orgId && entry.orgId !== orgId) {
        throw new AppWorkerError(
          "forbidden",
          `this pod's worker credential for "${slug}" belongs to a different org — this pod is not org ${orgId}'s execution home`,
          403,
        );
      }
      return await this.executor.invokeHandler(
        slug, handler, args, timeoutMs ?? this.opts.invokeTimeoutMs ?? INVOKE_TIMEOUT_MS,
      );
    } catch (err) {
      throw classifyWorkerError(err);
    }
  }

  /** The readiness line `/readyz` reports when app-worker mode is enabled —
   *  registered by server.ts the way every other capability seam is. */
  status(): CapabilityStatus {
    return {
      ready: true,
      tier: "basic",
      detail: `app workers armed (${this.entries.size} app${this.entries.size === 1 ? "" : "s"}; cron ${this.cron ? "on" : "off"})`,
    };
  }

  /** Pod shutdown / test teardown: stop timers, kill workers, release the store. */
  async close(): Promise<void> {
    this.cron?.stop();
    await this.executor.disposeAll();
    await this.cronStore?.close?.();
  }

  // ── arming ────────────────────────────────────────────────────────────────

  /** The executor's bundle seam: ensure the entry (mint + bridge) exists, then
   *  serve the materialized bundle. */
  private async resolveArmed(slug: string): Promise<ResolvedApp> {
    await this.entry(slug);
    return this.bundles.resolveApp(slug);
  }

  private entry(slug: string): Promise<AppEntry> {
    const existing = this.entries.get(slug);
    if (existing) return existing;
    const p = this.arm(slug);
    this.entries.set(slug, p);
    // A failed arm (control plane down, app not installed) must NOT wedge the
    // slug — drop the promise so the next invoke retries from scratch.
    p.catch(() => this.entries.delete(slug));
    return p;
  }

  private async arm(slug: string): Promise<AppEntry> {
    // Mint IS rotate (one live handle per org+app): whoever held the previous
    // handle loses it now — the one-execution-home rule enforced server-side.
    const minted = await this.control.apps.mintWorkerToken(slug);
    const app = await this.bundles.resolveApp(slug);
    const bridge = createCapabilityBridge({
      controlPlaneUrl: this.opts.controlPlaneUrl,
      workerToken: minted.token,
      slug: minted.appSlug || slug,
      manifest: app.manifest,
      orgId: minted.orgId,
      ...(this.cron ? { cron: this.cron } : {}),
      ...(this.opts.fetchImpl ? { fetchImpl: this.opts.fetchImpl } : {}),
    });
    // RE-ARM ON 401 (the e2e report's gap): mint is rotate, so ANOTHER pod
    // arming this (org, slug) kills OUR minted handle out from under us — every
    // data call would 401 forever while the cached arm pins the dead token.
    // When the control plane rejects the worker token, drop the cached arm so
    // the NEXT invoke re-mints (taking the handle back) instead of wedging.
    // The failing call itself still fails — its worker script already observed
    // the refusal — recovery is one invoke later, not a silent retry.
    this.bridges.set(slug, async (method, args) => {
      try {
        return await bridge(method, args);
      } catch (err) {
        if (isWorkerTokenRejection(err)) {
          this.bridges.delete(slug);
          this.entries.delete(slug);
          log.warn("worker token rejected by the control plane — dropping the arm to re-mint on next invoke", {
            app: slug,
            org_id: minted.orgId,
          });
        }
        throw err;
      }
    });
    log.info("app worker armed", {
      app: slug,
      org_id: minted.orgId,
      sha256: app.sha256.slice(0, 12),
      version: app.manifest.release?.version ?? null,
      rotated: minted.rotatedAt !== null,
    });
    return { orgId: minted.orgId, slug };
  }
}

// ── env wiring ──────────────────────────────────────────────────────────────

/** The mode switch: `ROTOR_APP_WORKERS=1` (or `true`). Off by default, so
 *  existing deployments are untouched. */
export function appWorkersEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  return env.ROTOR_APP_WORKERS === "1" || env.ROTOR_APP_WORKERS === "true";
}

/**
 * Build the pod's app-worker service from the environment, or null when the
 * mode is off. Uses the SAME control-plane coordinates the harness already
 * reads (`GLYPHH_CONTROL_URL` + `GLYPHH_RUNTIME_TOKEN` — harness/config.ts);
 * both are required, and a missing one logs loudly and stays off rather than
 * arming a surface that cannot mint. Durable cron rides the pod's existing
 * stator Postgres (the exact switch + DSN the thread store uses,
 * harness/threads.ts `statorConfigured`); a pod without a stator falls back to
 * in-memory schedules (this process only).
 */
export async function appWorkerServiceFromEnv(env: NodeJS.ProcessEnv = process.env): Promise<AppWorkerService | null> {
  if (!appWorkersEnabled(env)) return null;
  const controlPlaneUrl = (env.GLYPHH_CONTROL_URL ?? "").trim().replace(/\/+$/, "");
  const runtimeToken = (env.GLYPHH_RUNTIME_TOKEN ?? "").trim();
  if (!controlPlaneUrl || !runtimeToken) {
    log.error("ROTOR_APP_WORKERS is set but GLYPHH_CONTROL_URL / GLYPHH_RUNTIME_TOKEN are not both set — app workers stay OFF", {});
    return null;
  }
  const cacheDir = env.ROTOR_APP_CACHE_DIR ?? join(tmpdir(), "rrotor-app-bundles");

  let cronStore: CronStore;
  if (statorConfigured(env)) {
    try {
      cronStore = await PgCronStore.create({ url: env.ROTOR_STATOR_URL });
      log.info("app cron persistence enabled (stator)", {});
    } catch (err) {
      // Degrade, never block boot: the pod still serves invokes; schedules
      // just do not survive a restart until the stator is reachable.
      log.error("app cron store connect failed — falling back to in-memory schedules", {
        detail: (err as Error).message,
      });
      cronStore = new MemoryCronStore();
    }
  } else {
    cronStore = new MemoryCronStore();
  }
  return new AppWorkerService({ controlPlaneUrl, runtimeToken, cacheDir, cronStore });
}
