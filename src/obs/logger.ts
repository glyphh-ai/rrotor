/**
 * Structured logger — the runtime's diagnostic channel (BUILD_PLAN.md Phase 1).
 *
 * Zero-dependency. Emits one record per line to a sink (stderr by default, so it
 * never pollutes the CLI's human-facing stdout). JSON when `ROTOR_LOG_FORMAT=json`,
 * a compact pretty line otherwise. Bindings (e.g. `run_id`, `step_id`) attach via
 * `child()` so every log inside a run scope carries the run's identity.
 *
 * This is telemetry, NOT the control plane: the run loop's determinism (SPEC.md §6)
 * is unaffected by logging, and nothing here ever feeds back into control flow
 * ("telemetry only, never a control predicate", SPEC.md §17.6). The wall-clock
 * timestamp lives only in log lines, never in a `StepRecord`.
 */

export type LogLevel = "debug" | "info" | "warn" | "error";

const LEVEL_ORDER: Record<LogLevel, number> = { debug: 10, info: 20, warn: 30, error: 40 };

export type LogFields = Record<string, unknown>;

export interface Logger {
  debug(msg: string, fields?: LogFields): void;
  info(msg: string, fields?: LogFields): void;
  warn(msg: string, fields?: LogFields): void;
  error(msg: string, fields?: LogFields): void;
  /** Derive a logger that stamps `bindings` onto every record. */
  child(bindings: LogFields): Logger;
}

export interface LoggerOptions {
  /** Minimum level emitted. Default `info`. */
  level?: LogLevel;
  /** Wire format. Default `pretty`. */
  format?: "json" | "pretty";
  /** Line sink. Default writes to stderr. */
  write?: (line: string) => void;
  /** Fields stamped on every record from this logger. */
  bindings?: LogFields;
  /** Injectable clock (ms since epoch) — set in tests for deterministic output. */
  now?: () => number;
}

function defaultWrite(line: string): void {
  process.stderr.write(line + "\n");
}

class BaseLogger implements Logger {
  private readonly level: LogLevel;
  private readonly format: "json" | "pretty";
  private readonly sink: (line: string) => void;
  private readonly bindings: LogFields;
  private readonly now: () => number;

  constructor(opts: LoggerOptions) {
    this.level = opts.level ?? "info";
    this.format = opts.format ?? "pretty";
    this.sink = opts.write ?? defaultWrite;
    this.bindings = opts.bindings ?? {};
    this.now = opts.now ?? Date.now;
  }

  private emit(level: LogLevel, msg: string, fields?: LogFields): void {
    if (LEVEL_ORDER[level] < LEVEL_ORDER[this.level]) return;
    const ts = new Date(this.now()).toISOString();
    const merged = { ...this.bindings, ...(fields ?? {}) };
    this.sink(this.format === "json" ? renderJson(ts, level, msg, merged) : renderPretty(ts, level, msg, merged));
  }

  debug(msg: string, fields?: LogFields): void {
    this.emit("debug", msg, fields);
  }
  info(msg: string, fields?: LogFields): void {
    this.emit("info", msg, fields);
  }
  warn(msg: string, fields?: LogFields): void {
    this.emit("warn", msg, fields);
  }
  error(msg: string, fields?: LogFields): void {
    this.emit("error", msg, fields);
  }

  child(bindings: LogFields): Logger {
    return new BaseLogger({
      level: this.level,
      format: this.format,
      write: this.sink,
      now: this.now,
      bindings: { ...this.bindings, ...bindings },
    });
  }
}

function renderJson(ts: string, level: LogLevel, msg: string, fields: LogFields): string {
  // Deterministic key order: fixed head, then binding/field keys sorted.
  const head = { level, msg, ts } as Record<string, unknown>;
  for (const k of Object.keys(fields).sort()) head[k] = fields[k];
  return JSON.stringify(head);
}

function renderPretty(ts: string, level: LogLevel, msg: string, fields: LogFields): string {
  const kv = Object.keys(fields)
    .sort()
    .map((k) => `${k}=${fmtVal(fields[k])}`)
    .join(" ");
  const lvl = level.toUpperCase().padEnd(5);
  return `${ts} ${lvl} ${msg}${kv ? " " + kv : ""}`;
}

function fmtVal(v: unknown): string {
  if (typeof v === "string") return /\s/.test(v) ? JSON.stringify(v) : v;
  return JSON.stringify(v) ?? String(v);
}

/** Construct a logger from explicit options (used by tests and callers wanting
 *  their own sink). */
export function createLogger(opts: LoggerOptions = {}): Logger {
  return new BaseLogger(opts);
}

/** Construct a logger configured from the environment (`ROTOR_LOG_FORMAT`,
 *  `ROTOR_LOG_LEVEL`). The runtime's default diagnostic logger. `overrides` let
 *  callers (and tests) swap the sink/clock while keeping env-derived format/level. */
export function loggerFromEnv(
  env: NodeJS.ProcessEnv = process.env,
  overrides: Partial<LoggerOptions> = {},
): Logger {
  const format = env.ROTOR_LOG_FORMAT === "json" ? "json" : "pretty";
  const level = normalizeLevel(env.ROTOR_LOG_LEVEL);
  return new BaseLogger({ format, level, ...overrides });
}

function normalizeLevel(raw: string | undefined): LogLevel {
  switch ((raw ?? "").toLowerCase()) {
    case "debug":
      return "debug";
    case "warn":
      return "warn";
    case "error":
      return "error";
    default:
      return "info";
  }
}

/** The process-wide default diagnostic logger. */
export const log: Logger = loggerFromEnv();
