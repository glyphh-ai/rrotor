/**
 * app-cron.test.ts — the durable app scheduler (src/app-worker/cron.ts).
 *
 * The contract: desktop-parity call shapes ({ id, cron, handler, args? }),
 * 20-per-app cap, per-(org, app) scoping, minute-resolution firing through
 * the executor, per-row failure logging (the service never dies), and — the
 * durable part — persisted rows re-arm when a NEW service starts over the
 * same store (a pod restart). The cron parser accepts the classic 5-field
 * subset and rejects everything fancier with a precise message.
 */

import { describe, it, expect, afterEach, vi } from "vitest";
import { PGlite } from "@electric-sql/pglite";

import {
  AppCronService,
  MemoryCronStore,
  PgCronStore,
  MAX_SCHEDULES_PER_APP,
  parseCronExpression,
  validateCronExpression,
  nextCronFire,
} from "../../src/app-worker/cron.js";
import type { CronExecutor, CronScope, CronStore } from "../../src/app-worker/cron.js";
import type { PgLike } from "../../src/exec/pgvector-store.js";
import type { Logger, LogFields } from "../../src/obs/logger.js";

const SCOPE: CronScope = { orgId: "org-1", slug: "demo" };

/** A recording executor — what the schedules fire into. */
function recordingExecutor(impl?: (slug: string, name: string, args: unknown) => Promise<unknown>): {
  executor: CronExecutor;
  calls: Array<{ slug: string; name: string; args: unknown; timeoutMs: number | undefined }>;
} {
  const calls: Array<{ slug: string; name: string; args: unknown; timeoutMs: number | undefined }> = [];
  return {
    calls,
    executor: {
      invokeHandler(slug, name, args, timeoutMs) {
        calls.push({ slug, name, args, timeoutMs });
        return impl ? impl(slug, name, args) : Promise.resolve(null);
      },
    },
  };
}

/** A recording logger (the Logger seam, no stderr noise). */
function recordingLogger(): { logger: Logger; records: Array<{ level: string; msg: string; fields?: LogFields }> } {
  const records: Array<{ level: string; msg: string; fields?: LogFields }> = [];
  const make = (): Logger => ({
    debug: (msg, fields) => void records.push({ level: "debug", msg, ...(fields ? { fields } : {}) }),
    info: (msg, fields) => void records.push({ level: "info", msg, ...(fields ? { fields } : {}) }),
    warn: (msg, fields) => void records.push({ level: "warn", msg, ...(fields ? { fields } : {}) }),
    error: (msg, fields) => void records.push({ level: "error", msg, ...(fields ? { fields } : {}) }),
    child: () => make(),
  });
  return { logger: make(), records };
}

const services: AppCronService[] = [];
function makeService(opts: ConstructorParameters<typeof AppCronService>[0]): AppCronService {
  const s = new AppCronService(opts);
  services.push(s);
  return s;
}

afterEach(() => {
  for (const s of services.splice(0)) s.stop();
  vi.useRealTimers();
});

// ── the 5-field parser ──────────────────────────────────────────────────────

describe("cron expression subset", () => {
  it("accepts the classic forms", () => {
    for (const expr of ["* * * * *", "*/5 * * * *", "0 9 * * 1-5", "15,45 8-18/2 1 1,6 *", "0 0 1 * 0", "30 3 * * 7"]) {
      expect(validateCronExpression(expr), expr).toBe(true);
    }
  });

  it("rejects unsupported syntax with a precise reason", () => {
    expect(() => parseCronExpression("0 0 * * MON")).toThrow(/names, '\?', 'L', 'W', '#'/);
    expect(() => parseCronExpression("0 0 ? * *")).toThrow(/day-of-month/);
    expect(() => parseCronExpression("@hourly")).toThrow(/@-macros are not supported/);
    expect(() => parseCronExpression("0 0 0 * * *")).toThrow(/seconds fields are not supported/);
    expect(() => parseCronExpression("* * * *")).toThrow(/expected 5 fields .* got 4/);
    expect(() => parseCronExpression("61 * * * *")).toThrow(/values must be 0-59/);
    expect(() => parseCronExpression("* 25 * * *")).toThrow(/values must be 0-23/);
    expect(() => parseCronExpression("5-2 * * * *")).toThrow(/inverted/);
    expect(() => parseCronExpression("")).toThrow(/expected 5 fields/);
  });

  it("computes next occurrences (minute resolution, local time)", () => {
    // Thu Jan 1 2026, 12:00 local.
    const from = new Date(2026, 0, 1, 12, 0);
    const at = (expr: string) => nextCronFire(parseCronExpression(expr), from);

    expect(at("30 12 * * *")).toEqual(new Date(2026, 0, 1, 12, 30));
    expect(at("0 9 * * *")).toEqual(new Date(2026, 0, 2, 9, 0)); // 9:00 already past today
    expect(at("0 0 1 1 *")).toEqual(new Date(2027, 0, 1, 0, 0)); // next New Year
    expect(at("*/15 * * * *")).toEqual(new Date(2026, 0, 1, 12, 15));
    // "next" is strictly AFTER `from`, even when `from` itself matches.
    expect(at("0 12 * * *")).toEqual(new Date(2026, 0, 2, 12, 0));
  });

  it("uses vixie OR when both day fields are restricted", () => {
    // Thu Jan 1 2026. `0 0 13 * 5`: the 13th OR a Friday — Fri Jan 2 wins.
    const from = new Date(2026, 0, 1, 12, 0);
    expect(nextCronFire(parseCronExpression("0 0 13 * 5"), from)).toEqual(new Date(2026, 0, 2, 0, 0));
    // Restricted dom with unrestricted dow: plain AND — the 13th.
    expect(nextCronFire(parseCronExpression("0 0 13 * *"), from)).toEqual(new Date(2026, 0, 13, 0, 0));
  });

  it("returns null for an unsatisfiable date (Feb 30)", () => {
    expect(nextCronFire(parseCronExpression("0 0 30 2 *"), new Date(2026, 0, 1))).toBeNull();
  });
});

// ── the service over the in-memory store ────────────────────────────────────

describe("AppCronService", () => {
  it("schedules, lists (desktop shape), and cancels", async () => {
    const { executor } = recordingExecutor();
    const svc = makeService({ executor, store: new MemoryCronStore() });

    await svc.schedule(SCOPE, { id: "daily", cron: "0 9 * * *", handler: "digest", args: { n: 1 } });
    await svc.schedule(SCOPE, { id: "hourly", cron: "0 * * * *", handler: "poll" });

    expect(await svc.list(SCOPE)).toEqual([
      { id: "daily", cron: "0 9 * * *", handler: "digest", args: { n: 1 } },
      { id: "hourly", cron: "0 * * * *", handler: "poll" },
    ]);

    expect(await svc.cancel(SCOPE, { id: "daily" })).toEqual({ cancelled: true });
    expect(await svc.cancel(SCOPE, { id: "daily" })).toEqual({ cancelled: false });
    expect((await svc.list(SCOPE)).map((r) => r.id)).toEqual(["hourly"]);
  });

  it("validates id, handler, and cron like the desktop", async () => {
    const svc = makeService({ executor: recordingExecutor().executor, store: new MemoryCronStore() });
    await expect(svc.schedule(SCOPE, { cron: "* * * * *", handler: "h" })).rejects.toThrow(/id is required/);
    await expect(svc.schedule(SCOPE, { id: "bad id!", cron: "* * * * *", handler: "h" })).rejects.toThrow(/id is required/);
    await expect(svc.schedule(SCOPE, { id: "x", cron: "* * * * *" })).rejects.toThrow(/handler is required/);
    await expect(svc.schedule(SCOPE, { id: "x", cron: "not cron", handler: "h" })).rejects.toThrow(/invalid cron expression: "not cron"/);
  });

  it("caps at 20 schedules per app; re-scheduling an id is an upsert", async () => {
    const svc = makeService({ executor: recordingExecutor().executor, store: new MemoryCronStore() });
    for (let i = 0; i < MAX_SCHEDULES_PER_APP; i++) {
      await svc.schedule(SCOPE, { id: `job-${i}`, cron: "0 9 * * *", handler: "h" });
    }
    await expect(svc.schedule(SCOPE, { id: "one-more", cron: "0 9 * * *", handler: "h" })).rejects.toThrow(
      /schedule limit reached \(20 per app\)/,
    );
    // Same id again is a replace, not a 21st row.
    await svc.schedule(SCOPE, { id: "job-0", cron: "0 10 * * *", handler: "h2" });
    const rows = await svc.list(SCOPE);
    expect(rows).toHaveLength(MAX_SCHEDULES_PER_APP);
    expect(rows.find((r) => r.id === "job-0")).toMatchObject({ cron: "0 10 * * *", handler: "h2" });
    // Another app in the same org has its own budget.
    await svc.schedule({ ...SCOPE, slug: "other" }, { id: "fine", cron: "0 9 * * *", handler: "h" });
  });

  it("fires the worker handler through the executor with the standard timeout", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 0, 1, 12, 0, 30));
    const { executor, calls } = recordingExecutor();
    const svc = makeService({ executor, store: new MemoryCronStore() });

    await svc.schedule(SCOPE, { id: "tick", cron: "* * * * *", handler: "onTick", args: { a: 1 } });
    expect(calls).toHaveLength(0);

    await vi.advanceTimersByTimeAsync(31_000); // → 12:01:00
    expect(calls).toHaveLength(1);
    expect(calls[0]).toEqual({ slug: "demo", name: "onTick", args: { a: 1 }, timeoutMs: 60_000 });

    await vi.advanceTimersByTimeAsync(60_000); // → 12:02:00
    expect(calls).toHaveLength(2);

    // Cancel disarms: no third firing.
    await svc.cancel(SCOPE, { id: "tick" });
    await vi.advanceTimersByTimeAsync(120_000);
    expect(calls).toHaveLength(2);
  });

  it("logs a failing handler per-row and stays armed (never crashes)", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 0, 1, 12, 0, 0));
    const { logger, records } = recordingLogger();
    const { executor, calls } = recordingExecutor(() => Promise.reject(new Error("handler exploded")));
    const svc = makeService({ executor, store: new MemoryCronStore(), logger });

    await svc.schedule(SCOPE, { id: "flaky", cron: "* * * * *", handler: "boom" });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(calls).toHaveLength(1);
    const failure = records.find((r) => r.level === "error" && r.msg === "app cron firing failed");
    expect(failure?.fields).toMatchObject({ app: "demo", id: "flaky", handler: "boom", detail: "handler exploded" });

    // Still armed: the next minute fires again.
    await vi.advanceTimersByTimeAsync(60_000);
    expect(calls).toHaveLength(2);
  });

  it("re-arms persisted schedules on start (pod restart)", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 0, 1, 12, 0, 0));
    const store = new MemoryCronStore();

    // First life: schedule, then the pod "dies" (stop clears timers only).
    const first = makeService({ executor: recordingExecutor().executor, store });
    await first.schedule(SCOPE, { id: "survivor", cron: "* * * * *", handler: "onTick" });
    first.stop();

    // Second life over the SAME store.
    const { executor, calls } = recordingExecutor();
    const second = makeService({ executor, store });
    await second.start();
    expect(await second.list(SCOPE)).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(60_000);
    expect(calls).toHaveLength(1);
    expect(calls[0]).toMatchObject({ slug: "demo", name: "onTick" });
  });

  it("drops persisted rows that no longer validate instead of failing start", async () => {
    const store = new MemoryCronStore();
    await store.upsert({ orgId: "org-1", slug: "demo", id: "rotten", cron: "@hourly", handler: "h", args: null });
    await store.upsert({ orgId: "org-1", slug: "demo", id: "good", cron: "0 9 * * *", handler: "h", args: null });

    const { logger, records } = recordingLogger();
    const svc = makeService({ executor: recordingExecutor().executor, store, logger });
    await svc.start();

    expect((await svc.list(SCOPE)).map((r) => r.id)).toEqual(["good"]);
    expect(records.some((r) => r.level === "warn" && r.msg === "dropping invalid persisted schedule")).toBe(true);
  });

  it("keeps (org, app) scopes apart", async () => {
    const store = new MemoryCronStore();
    const svc = makeService({ executor: recordingExecutor().executor, store });
    await svc.schedule({ orgId: "org-1", slug: "demo" }, { id: "a", cron: "0 9 * * *", handler: "h" });
    await svc.schedule({ orgId: "org-2", slug: "demo" }, { id: "a", cron: "0 8 * * *", handler: "h" });

    expect(await svc.list({ orgId: "org-1", slug: "demo" })).toEqual([{ id: "a", cron: "0 9 * * *", handler: "h" }]);
    expect(await svc.list({ orgId: "org-2", slug: "demo" })).toEqual([{ id: "a", cron: "0 8 * * *", handler: "h" }]);
    await svc.cancel({ orgId: "org-2", slug: "demo" }, { id: "a" });
    expect(await svc.list({ orgId: "org-1", slug: "demo" })).toHaveLength(1);
  });
});

// ── the Postgres store (PGlite, no server required) ─────────────────────────

describe("PgCronStore", () => {
  const open: PGlite[] = [];
  afterEach(async () => {
    while (open.length) await open.pop()!.close();
  });

  async function pgStore(): Promise<CronStore> {
    const db = await PGlite.create();
    open.push(db);
    return PgCronStore.create({ client: db as unknown as PgLike });
  }

  it("round-trips upsert/list/delete/all with (org, app) scoping", async () => {
    const store = await pgStore();
    await store.upsert({ orgId: "org-1", slug: "demo", id: "daily", cron: "0 9 * * *", handler: "digest", args: { n: 1 } });
    await store.upsert({ orgId: "org-1", slug: "demo", id: "hourly", cron: "0 * * * *", handler: "poll", args: null });
    await store.upsert({ orgId: "org-2", slug: "demo", id: "daily", cron: "0 8 * * *", handler: "digest", args: null });

    expect(await store.list("org-1", "demo")).toEqual([
      { orgId: "org-1", slug: "demo", id: "daily", cron: "0 9 * * *", handler: "digest", args: { n: 1 } },
      { orgId: "org-1", slug: "demo", id: "hourly", cron: "0 * * * *", handler: "poll", args: null },
    ]);
    expect(await store.all()).toHaveLength(3);

    // Upsert replaces in place.
    await store.upsert({ orgId: "org-1", slug: "demo", id: "daily", cron: "30 9 * * *", handler: "digest2", args: null });
    expect((await store.list("org-1", "demo")).find((r) => r.id === "daily")).toMatchObject({ cron: "30 9 * * *", handler: "digest2" });

    expect(await store.delete("org-1", "demo", "hourly")).toBe(true);
    expect(await store.delete("org-1", "demo", "hourly")).toBe(false);
    expect(await store.all()).toHaveLength(2);
  });

  it("is idempotent to create (ensure-DDL) and survives a reopen on the same db", async () => {
    const db = await PGlite.create();
    open.push(db);
    const first = await PgCronStore.create({ client: db as unknown as PgLike });
    await first.upsert({ orgId: "org-1", slug: "demo", id: "keep", cron: "0 9 * * *", handler: "h", args: null });
    // A "restarted pod" creates a fresh store over the same database.
    const second = await PgCronStore.create({ client: db as unknown as PgLike });
    expect(await second.all()).toEqual([{ orgId: "org-1", slug: "demo", id: "keep", cron: "0 9 * * *", handler: "h", args: null }]);
  });

  it("drives the service end to end (schedule → restart → re-armed fire)", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 0, 1, 12, 0, 0));
    const store = await pgStore();

    const first = makeService({ executor: recordingExecutor().executor, store });
    await first.schedule(SCOPE, { id: "tick", cron: "* * * * *", handler: "onTick" });
    first.stop();

    const { executor, calls } = recordingExecutor();
    const second = makeService({ executor, store });
    await second.start();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(calls).toHaveLength(1);
  });
});
