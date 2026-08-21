/**
 * cron.ts — DURABLE app cron for the pod runtime (workpanel apps, slice 4).
 *
 * The pod-side counterpart of the desktop's app scheduler
 * (app/src/main/glyphh-app-services.ts cronSchedule/cronCancel/cronList):
 * same call shapes ({ id, cron, handler, args? }), same 20-per-app cap, same
 * behaviour — a schedule fires the app's WORKER HANDLER through the executor,
 * and a persisted schedule comes back to life when the pod restarts
 * ({@link AppCronService.start} re-arms every stored row, dropping the ones
 * that no longer validate — desktop parity).
 *
 * Persistence follows the repo's stator pattern (harness/threads.ts over
 * exec/pgvector-store.ts): the durable rows live in Postgres behind the same
 * {@link PgLike} client seam ({@link PgCronStore}, idempotent DDL at create,
 * PGlite-injectable in tests), with {@link MemoryCronStore} as the in-memory
 * test/dev impl behind the same narrow {@link CronStore} interface. Rows are
 * keyed per (org, app) — the same scope as the `gy_wk_` worker token — so a
 * shared pod never crosses schedules between orgs or apps.
 *
 * Cron expressions: the repo carries no cron dependency (the desktop uses
 * node-cron), so this implements the minimal CLASSIC 5-FIELD SUBSET —
 * `minute hour day-of-month month day-of-week`, each field `*`, `a`, `a-b`,
 * a `/n` step over `*` or a range, or a comma list of those; day-of-month/
 * day-of-week combine with vixie-cron OR semantics when both are restricted.
 * Everything else (seconds fields, names, `?`, `L`, `W`, `#`, `@macros`) is
 * REJECTED with a precise message rather than guessed at.
 *
 * Failure convention (the repo's reaper rule): a firing handler that throws or
 * times out is logged per-row and the schedule stays armed for its next
 * occurrence — a bad handler never takes the service (or the pod) down.
 */

import { connectPg, asJson } from "../exec/pgvector-store.js";
import type { PgLike } from "../exec/pgvector-store.js";
import { INVOKE_TIMEOUT_MS } from "./executor.js";
import { log } from "../obs/logger.js";
import type { Logger } from "../obs/logger.js";

/** Desktop parity (glyphh-app-services.ts MAX_SCHEDULES_PER_APP). */
export const MAX_SCHEDULES_PER_APP = 20;

/** How far {@link nextCronFire} searches before declaring the expression
 *  unsatisfiable (e.g. `0 0 30 2 *`): 366 days covers every leap-year shape. */
const SEARCH_LIMIT_DAYS = 366;

// ── the 5-field cron subset ─────────────────────────────────────────────────

/** A parsed 5-field expression: the allowed values per field, plus whether the
 *  two day fields were restricted (vixie OR applies only when BOTH are). */
export interface CronFields {
  minute: Set<number>;
  hour: Set<number>;
  dom: Set<number>;
  month: Set<number>;
  dow: Set<number>;
  domRestricted: boolean;
  dowRestricted: boolean;
}

interface FieldSpec {
  name: string;
  min: number;
  max: number;
}

const FIELD_SPECS: FieldSpec[] = [
  { name: "minute", min: 0, max: 59 },
  { name: "hour", min: 0, max: 23 },
  { name: "day-of-month", min: 1, max: 31 },
  { name: "month", min: 1, max: 12 },
  { name: "day-of-week", min: 0, max: 7 }, // 7 normalizes to 0 (Sunday)
];

function parseField(raw: string, spec: FieldSpec): { values: Set<number>; restricted: boolean } {
  const bad = (why: string) => new Error(`unsupported cron ${spec.name} field "${raw}": ${why}`);
  if (!raw) throw bad("empty field");
  if (!/^[0-9*/,-]+$/.test(raw)) {
    throw bad("only numbers, '*', ',', '-', '/' are supported (names, '?', 'L', 'W', '#' are not)");
  }
  const values = new Set<number>();
  let restricted = false;
  for (const part of raw.split(",")) {
    // part: `*` | `*/n` | `a` | `a-b` | `a-b/n`
    const m = /^(\*|\d+(?:-\d+)?)(?:\/(\d+))?$/.exec(part);
    if (!m) throw bad(`cannot parse "${part}"`);
    const [, rangeRaw, stepRaw] = m;
    const step = stepRaw === undefined ? 1 : Number(stepRaw);
    if (step < 1) throw bad("step must be >= 1");
    let lo = spec.min;
    let hi = spec.max;
    if (rangeRaw !== "*") {
      restricted = true;
      const [a, b] = rangeRaw.split("-").map(Number);
      lo = a;
      hi = b === undefined ? a : b;
      if (b === undefined && stepRaw !== undefined) hi = spec.max; // `a/n` = a..max by n (classic cron)
      if (lo < spec.min || hi > spec.max) throw bad(`values must be ${spec.min}-${spec.max}`);
      if (lo > hi) throw bad(`range ${lo}-${hi} is inverted`);
    } else if (stepRaw !== undefined && step > 1) {
      restricted = true; // `*/n` restricts (the degenerate `*/1` does not)
    }
    for (let v = lo; v <= hi; v += step) {
      // day-of-week 7 is Sunday, same as 0.
      values.add(spec.max === 7 && v === 7 ? 0 : v);
    }
  }
  return { values, restricted };
}

/** Parse a classic 5-field cron expression, or throw a precise refusal. */
export function parseCronExpression(expr: string): CronFields {
  const trimmed = String(expr ?? "").trim();
  if (trimmed.startsWith("@")) {
    throw new Error(`unsupported cron expression "${trimmed}": @-macros are not supported — use the 5-field form`);
  }
  const parts = trimmed.split(/\s+/).filter(Boolean);
  if (parts.length !== 5) {
    throw new Error(
      `unsupported cron expression "${trimmed}": expected 5 fields (minute hour day-of-month month day-of-week), got ${parts.length}` +
        (parts.length === 6 ? " — seconds fields are not supported" : ""),
    );
  }
  const [minute, hour, dom, month, dow] = parts.map((p, i) => parseField(p, FIELD_SPECS[i]));
  return {
    minute: minute.values,
    hour: hour.values,
    dom: dom.values,
    month: month.values,
    dow: dow.values,
    domRestricted: dom.restricted,
    dowRestricted: dow.restricted,
  };
}

/** True when `expr` parses under the supported subset. */
export function validateCronExpression(expr: string): boolean {
  try {
    parseCronExpression(expr);
    return true;
  } catch {
    return false;
  }
}

/** Does `d` (local time) satisfy the day constraints? Vixie semantics: when
 *  BOTH day fields are restricted, either may match; otherwise both apply
 *  (an unrestricted field matches everything anyway). */
function dayMatches(f: CronFields, d: Date): boolean {
  const domOk = f.dom.has(d.getDate());
  const dowOk = f.dow.has(d.getDay());
  if (f.domRestricted && f.dowRestricted) return domOk || dowOk;
  return domOk && dowOk;
}

/**
 * The next fire time strictly AFTER `from`, or null when no occurrence exists
 * within {@link SEARCH_LIMIT_DAYS} (an unsatisfiable date like Feb 30).
 * Minute resolution, local time — the same clock the desktop's node-cron uses.
 */
export function nextCronFire(fields: CronFields, from: Date = new Date()): Date | null {
  const t = new Date(from.getTime());
  t.setSeconds(0, 0);
  t.setMinutes(t.getMinutes() + 1);
  const limit = from.getTime() + SEARCH_LIMIT_DAYS * 24 * 60 * 60 * 1000;
  while (t.getTime() <= limit) {
    if (!fields.month.has(t.getMonth() + 1) || !dayMatches(fields, t)) {
      // Skip to the next day's 00:00 — day-level misses never match by minute.
      t.setHours(24, 0, 0, 0);
      continue;
    }
    if (!fields.hour.has(t.getHours())) {
      t.setHours(t.getHours() + 1, 0, 0, 0);
      continue;
    }
    if (!fields.minute.has(t.getMinutes())) {
      t.setMinutes(t.getMinutes() + 1);
      continue;
    }
    return t;
  }
  return null;
}

// ── the durable store ───────────────────────────────────────────────────────

/** One persisted schedule, scoped to (org, app) — the worker token's scope. */
export interface CronScheduleRow {
  orgId: string;
  slug: string;
  id: string;
  cron: string;
  handler: string;
  /** JSON-serializable handler args, or null. */
  args: unknown;
}

/** The narrow persistence seam — a Postgres impl for pods, memory for tests. */
export interface CronStore {
  /** Insert or replace the (orgId, slug, id) row. */
  upsert(row: CronScheduleRow): Promise<void>;
  /** Delete one row; false when it did not exist. */
  delete(orgId: string, slug: string, id: string): Promise<boolean>;
  /** The app's rows, id-ordered. */
  list(orgId: string, slug: string): Promise<CronScheduleRow[]>;
  /** Every row on this store (startup re-arm). */
  all(): Promise<CronScheduleRow[]>;
  close?(): Promise<void>;
}

/** In-memory {@link CronStore} — tests and podless dev (nothing survives). */
export class MemoryCronStore implements CronStore {
  private readonly rows = new Map<string, CronScheduleRow>();

  private key(orgId: string, slug: string, id: string): string {
    return `${orgId}\n${slug}\n${id}`;
  }

  async upsert(row: CronScheduleRow): Promise<void> {
    this.rows.set(this.key(row.orgId, row.slug, row.id), { ...row });
  }

  async delete(orgId: string, slug: string, id: string): Promise<boolean> {
    return this.rows.delete(this.key(orgId, slug, id));
  }

  async list(orgId: string, slug: string): Promise<CronScheduleRow[]> {
    return [...this.rows.values()]
      .filter((r) => r.orgId === orgId && r.slug === slug)
      .sort((a, b) => a.id.localeCompare(b.id))
      .map((r) => ({ ...r }));
  }

  async all(): Promise<CronScheduleRow[]> {
    return [...this.rows.values()].map((r) => ({ ...r }));
  }
}

// One flat pod-owned operational table (this is the POD's durable state, not
// server-owned tenant data — org isolation is a keyed column, the same shape
// as the desktop's app_schedules table plus the org scope).
const CRON_DDL = `
CREATE TABLE IF NOT EXISTS app_worker_schedules (
  org_id    TEXT NOT NULL,
  app_slug  TEXT NOT NULL,
  id        TEXT NOT NULL,
  cron      TEXT NOT NULL,
  handler   TEXT NOT NULL,
  args_json JSONB,
  updated_at BIGINT NOT NULL,
  PRIMARY KEY (org_id, app_slug, id)
);
`;

export interface PgCronStoreOptions {
  /** Inject a ready client (tests pass a PGlite instance). */
  client?: PgLike;
  /** Postgres connection string (the provisioned stator DSN). */
  url?: string;
}

/** The Postgres {@link CronStore} — same {@link PgLike} seam and idempotent
 *  ensure-DDL as the stator stores (exec/pgvector-store.ts, harness/threads.ts). */
export class PgCronStore implements CronStore {
  private constructor(
    private readonly db: PgLike,
    /** True when this store opened the connection (and must close it). */
    private readonly owned: boolean,
  ) {}

  static async create(opts: PgCronStoreOptions = {}): Promise<PgCronStore> {
    const db = opts.client ?? (await connectPg(opts.url, { max: 1 }));
    if (db.exec) await db.exec(CRON_DDL);
    else await db.query(CRON_DDL); // node-postgres runs multi-statement simple queries
    return new PgCronStore(db, !opts.client);
  }

  async upsert(row: CronScheduleRow): Promise<void> {
    await this.db.query(
      `INSERT INTO app_worker_schedules (org_id, app_slug, id, cron, handler, args_json, updated_at)
       VALUES ($1, $2, $3, $4, $5, $6, $7)
       ON CONFLICT (org_id, app_slug, id) DO UPDATE SET
         cron = excluded.cron, handler = excluded.handler,
         args_json = excluded.args_json, updated_at = excluded.updated_at`,
      [row.orgId, row.slug, row.id, row.cron, row.handler, row.args == null ? null : JSON.stringify(row.args), Date.now()],
    );
  }

  async delete(orgId: string, slug: string, id: string): Promise<boolean> {
    const { rows } = await this.db.query(
      "DELETE FROM app_worker_schedules WHERE org_id = $1 AND app_slug = $2 AND id = $3 RETURNING id",
      [orgId, slug, id],
    );
    return rows.length > 0;
  }

  async list(orgId: string, slug: string): Promise<CronScheduleRow[]> {
    const { rows } = await this.db.query(
      "SELECT org_id, app_slug, id, cron, handler, args_json FROM app_worker_schedules WHERE org_id = $1 AND app_slug = $2 ORDER BY id",
      [orgId, slug],
    );
    return rows.map((r) => this.toRow(r));
  }

  async all(): Promise<CronScheduleRow[]> {
    const { rows } = await this.db.query(
      "SELECT org_id, app_slug, id, cron, handler, args_json FROM app_worker_schedules ORDER BY org_id, app_slug, id",
    );
    return rows.map((r) => this.toRow(r));
  }

  private toRow(r: Record<string, unknown>): CronScheduleRow {
    return {
      orgId: String(r.org_id),
      slug: String(r.app_slug),
      id: String(r.id),
      cron: String(r.cron),
      handler: String(r.handler),
      args: r.args_json == null ? null : asJson(r.args_json),
    };
  }

  async close(): Promise<void> {
    if (!this.owned) return;
    if (this.db.close) await this.db.close();
    else if (this.db.end) await this.db.end();
  }
}

// ── the service ─────────────────────────────────────────────────────────────

/** The (org, app) a cron call is scoped to — carried by the capability bridge
 *  from its worker-token scope; never taken from app-supplied args. */
export interface CronScope {
  orgId: string;
  slug: string;
}

/** The one executor method cron needs — {@link AppWorkerExecutor} satisfies it;
 *  tests pass a recording stub. */
export interface CronExecutor {
  invokeHandler(slug: string, name: string, args?: unknown, timeoutMs?: number): Promise<unknown>;
}

export interface AppCronServiceOptions {
  executor: CronExecutor;
  store: CronStore;
  /** Handler cap per firing; defaults to the executor's standard 60s. */
  invokeTimeoutMs?: number;
  logger?: Logger;
}

interface ArmedTask {
  row: CronScheduleRow;
  fields: CronFields;
  timer: ReturnType<typeof setTimeout> | null;
}

/**
 * Schedules and fires app worker handlers, durably. Timers live in-process;
 * the rows live in the {@link CronStore} — {@link start} re-arms them after a
 * pod restart. All mutations write the store FIRST, then (re)arm, so a crash
 * between the two loses at most an in-memory timer the next boot restores.
 */
export class AppCronService {
  private readonly armed = new Map<string, ArmedTask>();
  private readonly logger: Logger;
  private readonly invokeTimeoutMs: number;
  private stopped = false;

  constructor(private readonly opts: AppCronServiceOptions) {
    this.logger = opts.logger ?? log.child({ mod: "app-cron" });
    this.invokeTimeoutMs = opts.invokeTimeoutMs ?? INVOKE_TIMEOUT_MS;
  }

  private key(orgId: string, slug: string, id: string): string {
    return `${orgId}\n${slug}\n${id}`;
  }

  /** Re-arm every persisted schedule (pod boot). Rows that no longer validate
   *  are deleted, not fatal — desktop parity (armAllSchedules). */
  async start(): Promise<void> {
    this.stopped = false;
    const rows = await this.opts.store.all();
    for (const row of rows) {
      let fields: CronFields;
      try {
        fields = parseCronExpression(row.cron);
      } catch (err) {
        this.logger.warn("dropping invalid persisted schedule", {
          org_id: row.orgId,
          app: row.slug,
          id: row.id,
          detail: (err as Error).message,
        });
        await this.opts.store.delete(row.orgId, row.slug, row.id).catch(() => undefined);
        continue;
      }
      this.arm(row, fields);
    }
  }

  /** The desktop's cron.schedule: upsert by id, cap 20 per app, arm. */
  async schedule(scope: CronScope, args: Record<string, unknown>): Promise<{ scheduled: true; id: string }> {
    const id = String(args.id ?? "").trim();
    const expr = String(args.cron ?? "").trim();
    const handler = String(args.handler ?? "").trim();
    if (!id || !/^[a-z0-9_-]{1,64}$/i.test(id)) throw new Error("id is required ([a-zA-Z0-9_-], max 64)");
    if (!handler) throw new Error("handler is required");
    let fields: CronFields;
    try {
      fields = parseCronExpression(expr);
    } catch (err) {
      throw new Error(`invalid cron expression: "${expr}" — ${(err as Error).message}`);
    }

    const existing = await this.opts.store.list(scope.orgId, scope.slug);
    if (existing.filter((r) => r.id !== id).length >= MAX_SCHEDULES_PER_APP) {
      throw new Error(`schedule limit reached (${MAX_SCHEDULES_PER_APP} per app)`);
    }

    const row: CronScheduleRow = {
      orgId: scope.orgId,
      slug: scope.slug,
      id,
      cron: expr,
      handler,
      args: args.args && typeof args.args === "object" ? args.args : null,
    };
    await this.opts.store.upsert(row);
    this.arm(row, fields);
    return { scheduled: true, id };
  }

  /** The desktop's cron.cancel: disarm + delete. */
  async cancel(scope: CronScope, args: Record<string, unknown>): Promise<{ cancelled: boolean }> {
    const id = String(args.id ?? "").trim();
    if (!id) throw new Error("id is required");
    this.disarm(scope.orgId, scope.slug, id);
    const cancelled = await this.opts.store.delete(scope.orgId, scope.slug, id);
    return { cancelled };
  }

  /** The desktop's cron.list shape: [{ id, cron, handler, args? }]. */
  async list(scope: CronScope): Promise<Array<{ id: string; cron: string; handler: string; args?: unknown }>> {
    const rows = await this.opts.store.list(scope.orgId, scope.slug);
    return rows.map((r) => ({
      id: r.id,
      cron: r.cron,
      handler: r.handler,
      ...(r.args != null ? { args: r.args } : {}),
    }));
  }

  /** Stop every timer (pod shutdown / test teardown). Persisted rows are
   *  untouched — they re-arm on the next {@link start}. */
  stop(): void {
    this.stopped = true;
    for (const task of this.armed.values()) {
      if (task.timer) clearTimeout(task.timer);
    }
    this.armed.clear();
  }

  private disarm(orgId: string, slug: string, id: string): void {
    const key = this.key(orgId, slug, id);
    const task = this.armed.get(key);
    if (task) {
      if (task.timer) clearTimeout(task.timer);
      this.armed.delete(key);
    }
  }

  private arm(row: CronScheduleRow, fields: CronFields): void {
    this.disarm(row.orgId, row.slug, row.id);
    const task: ArmedTask = { row, fields, timer: null };
    this.armed.set(this.key(row.orgId, row.slug, row.id), task);
    this.armNext(task);
  }

  /** Compute the next occurrence and sleep to it. Long waits are chunked under
   *  the 32-bit setTimeout ceiling and re-checked on wake. */
  private armNext(task: ArmedTask): void {
    const next = nextCronFire(task.fields, new Date());
    if (!next) {
      this.logger.warn("schedule has no future occurrence — leaving it dormant", {
        org_id: task.row.orgId,
        app: task.row.slug,
        id: task.row.id,
        cron: task.row.cron,
      });
      return;
    }
    this.sleepUntil(task, next.getTime());
  }

  private sleepUntil(task: ArmedTask, target: number): void {
    const MAX_DELAY = 2 ** 31 - 1;
    const delay = Math.min(Math.max(target - Date.now(), 0), MAX_DELAY);
    task.timer = setTimeout(() => {
      task.timer = null;
      if (this.stopped || this.armed.get(this.key(task.row.orgId, task.row.slug, task.row.id)) !== task) return;
      if (Date.now() < target) {
        this.sleepUntil(task, target); // chunked long wait — keep sleeping
        return;
      }
      this.fire(task);
      this.armNext(task);
    }, delay);
    (task.timer as { unref?: () => void }).unref?.();
  }

  /** Fire one occurrence. Never throws; failures are logged per-row (the
   *  reaper convention) and the schedule stays armed. */
  private fire(task: ArmedTask): void {
    const { row } = task;
    void this.opts.executor
      .invokeHandler(row.slug, row.handler, row.args ?? {}, this.invokeTimeoutMs)
      .catch((err: unknown) => {
        this.logger.error("app cron firing failed", {
          org_id: row.orgId,
          app: row.slug,
          id: row.id,
          handler: row.handler,
          detail: err instanceof Error ? err.message : String(err),
        });
      });
  }
}
