/**
 * Log drains (BUILD_PLAN.md Phase 3) — the eighth capability seam.
 *
 * A drain streams the run's `StepRecord` event stream (§5.4) to an external sink
 * for audit / FinOps / observability. The design is governed by two invariants:
 *
 *  1. **Telemetry only, never control** (SPEC.md §17.6): a drain observes; it can
 *     never change a run's outputs or transitions. `emit` is fire-and-forget —
 *     it enqueues and returns, never blocks the run loop, never throws.
 *  2. **Determinism-neutral**: the executor calls `emit` only on FRESH execution
 *     (never on replay), so replaying a run does not re-emit, and the drain never
 *     touches the recorded `StepRecord`.
 *
 * Reliability lives in {@link BufferedDrain}: a bounded buffer (drop-oldest under
 * backpressure, with a counter), batched delivery, and retry-with-backoff. The
 * concrete sinks are {@link HttpDrain} (batched NDJSON POST) and {@link FileDrain}
 * (append NDJSON). {@link NoopDrain} is the basic-tier default when nothing is
 * configured.
 */

import { appendFile } from "node:fs/promises";

import type { StepRecord } from "../types.js";
import type { CapabilityStatus } from "../runtime/registry.js";
import type { DrainEnvelope, DrainPlugin } from "./interfaces.js";
import { loggerFromEnv, type Logger } from "../obs/logger.js";

const ENVELOPE_TYPE = "com.openrotor.step.v0";
const REDACTED = "[redacted]";

/**
 * Serialize a `StepRecord` into a drain envelope, redacting the named top-level
 * `output` fields. Pure and deterministic — the emit timestamp is added (if at
 * all) by the sink, so the envelope content is reproducible.
 */
export function toEnvelope(rec: StepRecord, redact?: ReadonlySet<string>): DrainEnvelope {
  const env: DrainEnvelope = {
    type: ENVELOPE_TYPE,
    run_id: rec.run_id,
    step_id: rec.step_id,
    attempt: rec.attempt,
    logical_tick: rec.logical_tick,
    status: rec.status,
  };
  if (rec.space_id !== undefined) env.space_id = rec.space_id;
  if (rec.principal) {
    env.principal = { id: rec.principal.id, kind: rec.principal.kind };
    if (rec.principal.scopes) env.principal.scopes = rec.principal.scopes;
  }
  if (rec.agent_identity) env.agent = { ref: rec.agent_identity.ref, run_id: rec.agent_identity.run_id };
  if (rec.usage) env.usage = rec.usage;
  if (rec.frames) env.frames = rec.frames;
  if (rec.error) env.error = { name: rec.error.name, cause: rec.error.cause };
  if (rec.output) env.output = redactFields(rec.output, redact);
  return env;
}

function redactFields(output: Record<string, unknown>, redact?: ReadonlySet<string>): Record<string, unknown> {
  if (!redact || redact.size === 0) return output;
  const out: Record<string, unknown> = {};
  for (const k of Object.keys(output)) out[k] = redact.has(k) ? REDACTED : output[k];
  return out;
}

// ── the no-op default ────────────────────────────────────────────────────────

/** The basic-tier default: a ready seam that forwards nowhere. */
export class NoopDrain implements DrainPlugin {
  readonly name = "drain";
  emit(_rec: StepRecord): void {
    /* no sink configured */
  }
  async flush(): Promise<void> {
    /* nothing buffered */
  }
  async close(): Promise<void> {
    /* nothing to release */
  }
  status(): CapabilityStatus {
    return { ready: true, detail: "no drain configured", tier: "basic" };
  }
}

// ── the buffered base ─────────────────────────────────────────────────────────

export interface BufferedDrainOptions {
  /** Envelopes per delivered batch. Default 50. */
  batchSize?: number;
  /** Max buffered envelopes before drop-oldest kicks in. Default 10 000. */
  maxBuffer?: number;
  /** Delivery attempts per batch before the batch is dropped. Default 3. */
  maxRetries?: number;
  /** Top-level `output` field names to redact from every envelope. */
  redact?: ReadonlySet<string>;
  /** Backoff for retry attempt `n` (0-based). Default `100 * 2^n` ms. */
  backoffMs?: (attempt: number) => number;
  /** Injectable sleep (tests). */
  sleep?: (ms: number) => Promise<void>;
  logger?: Logger;
  /** Short label for `status().detail`. */
  label?: string;
  /** Ship automatically once a full batch accumulates. Default true; disable to
   *  control flushing explicitly (tests). */
  autoFlush?: boolean;
}

const realSleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

/** Buffering + backpressure + batched retry, shared by the concrete sinks. */
export abstract class BufferedDrain implements DrainPlugin {
  readonly name = "drain";
  private readonly buffer: DrainEnvelope[] = [];
  private readonly batchSize: number;
  private readonly maxBuffer: number;
  private readonly maxRetries: number;
  private readonly redact?: ReadonlySet<string>;
  private readonly backoffMs: (attempt: number) => number;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly log: Logger;
  private readonly label: string;
  private readonly autoFlush: boolean;
  private dropped = 0;
  private flushing = false;

  constructor(opts: BufferedDrainOptions = {}) {
    this.batchSize = opts.batchSize ?? 50;
    this.maxBuffer = opts.maxBuffer ?? 10_000;
    this.maxRetries = opts.maxRetries ?? 3;
    this.redact = opts.redact;
    this.backoffMs = opts.backoffMs ?? ((n) => 100 * 2 ** n);
    this.sleep = opts.sleep ?? realSleep;
    this.log = opts.logger ?? loggerFromEnv();
    this.label = opts.label ?? "drain";
    this.autoFlush = opts.autoFlush ?? true;
  }

  /** Deliver one batch to the sink. Throws on failure (triggers retry). */
  protected abstract ship(batch: DrainEnvelope[]): Promise<void>;

  emit(rec: StepRecord): void {
    try {
      if (this.buffer.length >= this.maxBuffer) {
        this.buffer.shift();
        this.dropped++;
        if (this.dropped === 1 || this.dropped % 1000 === 0) {
          this.log.warn("drain buffer full, dropping oldest", { dropped: this.dropped });
        }
      }
      this.buffer.push(toEnvelope(rec, this.redact));
      // Auto-ship once a full batch has accumulated, without blocking the caller.
      if (this.autoFlush && this.buffer.length >= this.batchSize && !this.flushing) void this.flush();
    } catch (err) {
      // A drain must never break a run.
      this.log.error("drain emit failed", { detail: (err as Error).message });
    }
  }

  async flush(): Promise<void> {
    if (this.flushing) return;
    this.flushing = true;
    try {
      while (this.buffer.length > 0) {
        const batch = this.buffer.splice(0, this.batchSize);
        await this.shipWithRetry(batch);
      }
    } finally {
      this.flushing = false;
    }
  }

  private async shipWithRetry(batch: DrainEnvelope[]): Promise<void> {
    for (let attempt = 0; ; attempt++) {
      try {
        await this.ship(batch);
        return;
      } catch (err) {
        if (attempt >= this.maxRetries - 1) {
          this.dropped += batch.length;
          this.log.warn("drain batch dropped after retries", {
            detail: (err as Error).message,
            dropped: this.dropped,
            batch: batch.length,
          });
          return;
        }
        await this.sleep(this.backoffMs(attempt));
      }
    }
  }

  async close(): Promise<void> {
    await this.flush();
  }

  /** Buffered/dropped counters for tests + operators. */
  stats(): { buffered: number; dropped: number } {
    return { buffered: this.buffer.length, dropped: this.dropped };
  }

  status(): CapabilityStatus {
    return { ready: true, detail: `${this.label} (dropped ${this.dropped})`, tier: "basic" };
  }
}

// ── HTTP sink ─────────────────────────────────────────────────────────────────

export interface HttpDrainOptions extends BufferedDrainOptions {
  url: string;
  token?: string;
}

/** POSTs batches as newline-delimited JSON to an HTTP endpoint. */
export class HttpDrain extends BufferedDrain {
  private readonly url: string;
  private readonly token?: string;

  constructor(opts: HttpDrainOptions) {
    super({ ...opts, label: `http:${opts.url}` });
    this.url = opts.url;
    this.token = opts.token;
  }

  protected async ship(batch: DrainEnvelope[]): Promise<void> {
    const body = batch.map((e) => JSON.stringify(e)).join("\n") + "\n";
    const headers: Record<string, string> = { "content-type": "application/x-ndjson" };
    if (this.token) headers.authorization = `Bearer ${this.token}`;
    const res = await fetch(this.url, { method: "POST", headers, body });
    if (!res.ok) throw new Error(`drain sink responded ${res.status}`);
  }
}

// ── file sink ─────────────────────────────────────────────────────────────────

/** Appends batches as newline-delimited JSON to a local file (sidecar tailing). */
export class FileDrain extends BufferedDrain {
  private readonly path: string;

  constructor(path: string, opts: BufferedDrainOptions = {}) {
    super({ ...opts, label: `file:${path}` });
    this.path = path;
  }

  protected async ship(batch: DrainEnvelope[]): Promise<void> {
    await appendFile(this.path, batch.map((e) => JSON.stringify(e)).join("\n") + "\n");
  }
}

// ── env wiring ────────────────────────────────────────────────────────────────

/**
 * Construct a drain from the environment. `ROTOR_DRAIN_URL` selects the HTTP sink
 * (`ROTOR_DRAIN_TOKEN` for bearer auth), else `ROTOR_DRAIN_FILE` selects the file
 * sink, else a {@link NoopDrain}. `ROTOR_DRAIN_BATCH`, `ROTOR_DRAIN_BUFFER`, and
 * `ROTOR_DRAIN_REDACT` (comma-separated field names) tune behavior.
 */
export function drainFromEnv(env: NodeJS.ProcessEnv = process.env, logger?: Logger): DrainPlugin {
  const redactList = (env.ROTOR_DRAIN_REDACT ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const opts: BufferedDrainOptions = {
    batchSize: env.ROTOR_DRAIN_BATCH ? Number(env.ROTOR_DRAIN_BATCH) : undefined,
    maxBuffer: env.ROTOR_DRAIN_BUFFER ? Number(env.ROTOR_DRAIN_BUFFER) : undefined,
    redact: redactList.length ? new Set(redactList) : undefined,
    logger,
  };
  if (env.ROTOR_DRAIN_URL) return new HttpDrain({ url: env.ROTOR_DRAIN_URL, token: env.ROTOR_DRAIN_TOKEN, ...opts });
  if (env.ROTOR_DRAIN_FILE) return new FileDrain(env.ROTOR_DRAIN_FILE, opts);
  return new NoopDrain();
}
