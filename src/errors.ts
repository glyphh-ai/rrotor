/**
 * The rrotor error taxonomy — the single, canonical vocabulary of failure for
 * the whole runtime (the "reflector" every layer reports through). Enterprise
 * operability rests on three properties this module guarantees:
 *
 *  1. **Known taxonomy.** Every failure has a stable `code` drawn from a closed
 *     {@link CATALOG}, tagged with a {@link ErrorCategory}, whether it is
 *     `retryable`, a {@link Severity}, an HTTP status, a one-line `summary`, and a
 *     concrete `remediation`. No anonymous `Error("boom")` reaches a boundary.
 *
 *  2. **Traceability & supportability.** {@link RotorError} carries structured,
 *     redaction-safe `context` and a `cause` chain, and renders to a machine
 *     payload ({@link RotorError.toTelemetry}) that a human or an AI dev-ops agent
 *     can pivot on: the `code` links straight to the catalog entry (docs/errors.md)
 *     with the fix.
 *
 *  3. **Determinism is preserved.** The taxonomy is telemetry-grade *and*
 *     control-safe: `code` is a stable string (no wall-clock, no RNG), and only the
 *     deterministic `{name, cause}` projection ({@link RotorError.toStepError})
 *     enters the append-only tape. The rich fields (category, severity,
 *     remediation, context, trace ids) ride on logs and the drain — telemetry only,
 *     never a control predicate (SPEC.md §17.6). `RotorError.name === code`, so the
 *     executor's existing retry/catch matching (which keys off the error name) and
 *     the recorded `StepError.name` are unchanged.
 */

import type { StepError } from "./types.js";

/** The top-level failure class — how an operator should *respond*, not where it
 *  came from. Chosen so a single switch drives triage. */
export type ErrorCategory =
  | "validation" // bad caller input or spec — reject, don't retry
  | "config" // misconfiguration / missing wiring — fix the deployment
  | "policy" // governance / authorization denial — grant or stop
  | "grounding" // memory / HDC / space integrity — data problem
  | "transport" // external I/O (model, tool, drain, network) — often retryable
  | "persistence" // the stator / durable store — often retryable
  | "capacity" // budgets, limits, timeouts — back off / raise the ceiling
  | "determinism" // replay / merge / translation invariant — a correctness bug
  | "escalation" // an expected control signal (e.g. frontier declined)
  | "internal"; // an unexpected defect — page someone

export type Severity = "warning" | "error" | "fatal";

/** The closed set of runtime error codes. Adding a failure mode means adding a
 *  code here AND an entry to {@link CATALOG} (a test enforces the 1:1). */
export type ErrorCode =
  | "E_MISSING_INPUT"
  | "E_TYPE"
  | "E_SCHEMA"
  | "OUT_OF_SCHEMA"
  | "E_UNKNOWN_STEP"
  | "E_NO_HANDLER"
  | "E_NO_TOOL"
  | "E_POLICY_DENIED"
  | "E_SCOPE_EXCEEDED"
  | "E_BUDGET_EXCEEDED"
  | "E_TIMEOUT"
  | "E_UNGROUNDED"
  | "E_SPACE_MISMATCH"
  | "E_TRANSPORT"
  | "E_TOOL"
  | "E_STATOR"
  | "E_UNTRANSLATABLE"
  | "E_UNMERGEABLE"
  | "E_REPLAY_DIVERGENCE"
  | "FrontierDeclined"
  | "E_HANDLER"
  | "E_FAILED"
  | "E_DEFAULT"
  | "E_INTERNAL";

/** A catalog entry: the operability metadata for one code. `summary` and
 *  `remediation` are the human/agent-facing support text (mirrored in
 *  docs/errors.md). */
export interface CatalogEntry {
  category: ErrorCategory;
  /** Safe to transparently retry (idempotent + transient cause). */
  retryable: boolean;
  severity: Severity;
  /** Suggested HTTP status when surfaced over the wire (§ server). */
  httpStatus: number;
  summary: string;
  remediation: string;
}

/** The closed catalog. This is the source of truth; docs/errors.md renders it. */
export const CATALOG: Readonly<Record<ErrorCode, CatalogEntry>> = {
  E_MISSING_INPUT: {
    category: "validation",
    retryable: false,
    severity: "error",
    httpStatus: 400,
    summary: "A required `in` reference resolved to nothing.",
    remediation: "Provide the missing input, or mark it optional in spec.inputs. Check the `$.` reference path in the failing step's `in`.",
  },
  E_TYPE: {
    category: "validation",
    retryable: false,
    severity: "error",
    httpStatus: 400,
    summary: "A value did not match its declared type.",
    remediation: "Coerce the value to the declared type upstream, or widen the type in the spec. The context carries the expected vs actual type.",
  },
  E_SCHEMA: {
    category: "validation",
    retryable: false,
    severity: "error",
    httpStatus: 400,
    summary: "The rotor document failed schema or static-graph validation.",
    remediation: "Run `rotor validate`; fix the reported path. Common causes: an unknown step type, a dangling `next`, or a gate without spec.space.",
  },
  OUT_OF_SCHEMA: {
    category: "validation",
    retryable: false,
    severity: "error",
    httpStatus: 422,
    summary: "A constrained decode produced a value outside the closed schema (§7.10).",
    remediation: "This is the lattice refusing to invent an option. Widen the `ops`/enum if the value is legitimate, else treat as a genuine reject and refine the plan.",
  },
  E_UNKNOWN_STEP: {
    category: "config",
    retryable: false,
    severity: "error",
    httpStatus: 500,
    summary: "A `next`/entry referenced a step id that does not exist.",
    remediation: "Fix the step graph: every `next` and `entry` must name a declared step or the reserved `end`. `rotor validate` catches this before run.",
  },
  E_NO_HANDLER: {
    category: "config",
    retryable: false,
    severity: "error",
    httpStatus: 501,
    summary: "No handler is registered for the step's type.",
    remediation: "Register a handler for this step type, or remove the step. If it is a premium step type, ensure the premium handler bundle is installed.",
  },
  E_NO_TOOL: {
    category: "config",
    retryable: false,
    severity: "error",
    httpStatus: 404,
    summary: "A `tool` step named a method not present in the connections registry.",
    remediation: "Register the tool/MCP method before the run, or correct the tool name. `connections.listTools()` shows what is available.",
  },
  E_POLICY_DENIED: {
    category: "policy",
    retryable: false,
    severity: "error",
    httpStatus: 403,
    summary: "Governance denied the step (step type, tool, model, or scope not granted).",
    remediation: "Grant the capability in spec.access / the principal's grant set, or remove the step. The context names the denied capability.",
  },
  E_SCOPE_EXCEEDED: {
    category: "capacity",
    retryable: false,
    severity: "error",
    httpStatus: 429,
    summary: "A grant scope limit was exceeded.",
    remediation: "Raise the scope limit for this principal/rotor, or reduce the work. Distinct from E_BUDGET_EXCEEDED (attention) — this is an authz scope.",
  },
  E_BUDGET_EXCEEDED: {
    category: "capacity",
    retryable: false,
    severity: "error",
    httpStatus: 429,
    summary: "The attention budget (revolutions / tokens) was exhausted (§10.4).",
    remediation: "Raise spec.attention.budget, or set on_exhausted: escalate so the run climbs the ladder instead of failing. The context carries the budget dimension.",
  },
  E_TIMEOUT: {
    category: "capacity",
    retryable: true,
    severity: "error",
    httpStatus: 504,
    summary: "An operation exceeded its time bound.",
    remediation: "The runtime retries transient timeouts with backoff. Persisting: raise the timeout, check the downstream latency, or route to a faster lane.",
  },
  E_UNGROUNDED: {
    category: "grounding",
    retryable: false,
    severity: "error",
    httpStatus: 422,
    summary: "A ground gate found no supporting fact for the claim (§6.3).",
    remediation: "This is the anti-fabrication backstop working. Write the supporting fact to the stator, or accept the refusal. The context carries (entity, role).",
  },
  E_SPACE_MISMATCH: {
    category: "grounding",
    retryable: false,
    severity: "fatal",
    httpStatus: 409,
    summary: "A cross-space HDC bind was attempted (§15.4).",
    remediation: "All grounding in one run must share a space_id = sha256(vector_dim, encoder_seed, roles_config). Do not mix spaces; re-encode into the run's space.",
  },
  E_TRANSPORT: {
    category: "transport",
    retryable: true,
    severity: "error",
    httpStatus: 502,
    summary: "An external dependency call failed at the transport layer.",
    remediation: "The runtime retries with backoff. Persisting: check the endpoint URL, credentials, and the environment's network policy (see the proxy README).",
  },
  E_TOOL: {
    category: "transport",
    retryable: true,
    severity: "error",
    httpStatus: 502,
    summary: "A registered tool/MCP method raised while executing.",
    remediation: "Inspect the tool's error in the context/cause. Retry if transient; otherwise fix the tool inputs or the downstream service. Idempotent tools are safe to retry.",
  },
  E_STATOR: {
    category: "persistence",
    retryable: true,
    severity: "error",
    httpStatus: 503,
    summary: "The durable stator (SQLite/Postgres) failed a read or write.",
    remediation: "Check ROTOR_STATOR_URL and DB reachability/credentials. Writes are mirrored and retried; a persistent failure surfaces on flush. The run's in-memory mirror stays authoritative.",
  },
  E_UNTRANSLATABLE: {
    category: "config",
    retryable: false,
    severity: "error",
    httpStatus: 501,
    summary: "The gateway has no translation for this (from, to) pair (§8.3).",
    remediation: "Add the missing entry to the gateway translation table, or route through a supported provider pair. Never a silent drop — this is the explicit refusal.",
  },
  E_UNMERGEABLE: {
    category: "determinism",
    retryable: false,
    severity: "fatal",
    httpStatus: 500,
    summary: "Two parallel branches produced conflicting writes to the same cell with no reducer (§7.17).",
    remediation: "Declare a reducer for the contended state key, or partition the branches so they write disjoint cells. The context names the cell.",
  },
  E_REPLAY_DIVERGENCE: {
    category: "determinism",
    retryable: false,
    severity: "fatal",
    httpStatus: 500,
    summary: "Replay produced a different result than the recorded tape (§5.4).",
    remediation: "A determinism violation — usually wall-clock/RNG leaking into the control plane, or an unpinned definition version. Pin the version; audit the diverging step for non-deterministic inputs.",
  },
  FrontierDeclined: {
    category: "escalation",
    retryable: false,
    severity: "warning",
    httpStatus: 503,
    summary: "The frontier lane declined the escalation (§9).",
    remediation: "Expected control signal, not a defect: the run falls back to the configured lower rung (e.g. human). Ensure a fallback is set on the escalate step.",
  },
  E_HANDLER: {
    category: "internal",
    retryable: false,
    severity: "error",
    httpStatus: 500,
    summary: "A step handler threw a non-taxonomy error.",
    remediation: "A defect in the handler — it should throw a RotorError. Inspect the cause chain; file with the run_id + step_id + trace_id from the log record.",
  },
  E_FAILED: {
    category: "internal",
    retryable: false,
    severity: "error",
    httpStatus: 500,
    summary: "A generic step failure with no more specific code.",
    remediation: "Prefer a specific code. If you see this, the throw site needs a taxonomy code; capture the cause and report it.",
  },
  E_DEFAULT: {
    category: "internal",
    retryable: false,
    severity: "error",
    httpStatus: 500,
    summary: "A fallthrough default error.",
    remediation: "Should not occur in normal operation; indicates an unhandled branch. Report with the run_id + trace_id.",
  },
  E_INTERNAL: {
    category: "internal",
    retryable: false,
    severity: "fatal",
    httpStatus: 500,
    summary: "An unexpected internal error (an unnormalized throw).",
    remediation: "A bug. The cause chain has the original error; report with the run_id + step_id + trace_id and the stack from the log record.",
  },
};

const ALL_CODES = Object.keys(CATALOG) as ErrorCode[];

/** Look up a code's operability metadata; unknown/legacy strings degrade to an
 *  `internal` entry rather than throwing (never let describing an error error). */
export function describe(code: string): CatalogEntry & { code: string } {
  const entry = (CATALOG as Record<string, CatalogEntry>)[code];
  if (entry) return { code, ...entry };
  return { code, ...CATALOG.E_INTERNAL, summary: `Unrecognized error code \`${code}\`.` };
}

/** Every catalog entry as a flat array — the machine-readable catalog an operator
 *  or AI dev-ops agent consumes (also the source for docs/errors.md). */
export function errorCatalog(): Array<CatalogEntry & { code: ErrorCode }> {
  return ALL_CODES.map((code) => ({ code, ...CATALOG[code] }));
}

export function isErrorCode(x: string): x is ErrorCode {
  return Object.prototype.hasOwnProperty.call(CATALOG, x);
}

export interface RotorErrorOptions {
  /** Structured, redaction-safe context (ids, names, counts — no secrets, no PII). */
  context?: Record<string, unknown>;
  /** The underlying error/value that triggered this one. */
  cause?: unknown;
}

/**
 * The runtime's structured error. Construct with a taxonomy `code`; category,
 * retryability, severity, HTTP status, and remediation are pulled from the
 * {@link CATALOG}. `name === code` so downstream matching and recording keep
 * working unchanged.
 */
export class RotorError extends Error {
  /** The catalogued {@link ErrorCode} when known; an uncatalogued domain/handler
   *  error name is tolerated (it degrades via {@link describe} to an `internal`
   *  descriptor) so `retry`/`catch` rules that match arbitrary names keep working. */
  readonly code: string;
  readonly category: ErrorCategory;
  readonly retryable: boolean;
  readonly severity: Severity;
  readonly httpStatus: number;
  readonly remediation: string;
  readonly summary: string;
  readonly context?: Record<string, unknown>;
  override readonly cause?: unknown;

  // `ErrorCode | (string & {})` keeps autocomplete for catalogued codes while still
  // accepting an uncatalogued name (a handler's own error class).
  constructor(code: ErrorCode | (string & {}), detail?: string, opts: RotorErrorOptions = {}) {
    const meta = describe(code);
    super(detail ?? meta.summary);
    this.name = code; // so `err.name` is the code (executor matches retry/catch on it)
    this.code = code;
    this.category = meta.category;
    this.retryable = meta.retryable;
    this.severity = meta.severity;
    this.httpStatus = meta.httpStatus;
    this.remediation = meta.remediation;
    this.summary = meta.summary;
    if (opts.context) this.context = opts.context;
    if (opts.cause !== undefined) this.cause = opts.cause;
  }

  /** The DETERMINISTIC projection that enters the append-only tape (§5.4): only
   *  the stable code + cause string. No wall-clock, no context, no stack. */
  toStepError(): StepError {
    return { name: this.code, cause: this.message };
  }

  /** The rich, machine-readable telemetry payload for logs and the drain — the
   *  supportability surface. NEVER recorded into the tape or read by control. */
  toTelemetry(): Record<string, unknown> {
    return {
      code: this.code,
      category: this.category,
      severity: this.severity,
      retryable: this.retryable,
      http_status: this.httpStatus,
      detail: this.message,
      remediation: this.remediation,
      ...(this.context ? { context: this.context } : {}),
      ...(this.cause !== undefined ? { cause: causeString(this.cause) } : {}),
    };
  }

  /**
   * Normalize any thrown value into a RotorError. An existing RotorError passes
   * through; an Error whose `name` is a known code is rebuilt with that code
   * (preserving the taxonomy across a throw); anything else becomes `fallback`
   * (default `E_INTERNAL`) with the original attached as `cause`.
   */
  static from(value: unknown, fallback: ErrorCode = "E_INTERNAL"): RotorError {
    if (value instanceof RotorError) return value;
    if (value instanceof Error) {
      // Preserve any meaningful thrown name as the code (catalogued or not) so
      // `retry`/`catch` matching is unchanged; only a generic/absent name falls back.
      const code = value.name && value.name !== "Error" ? value.name : fallback;
      return new RotorError(code, value.message, { cause: value });
    }
    return new RotorError(fallback, String(value), { cause: value });
  }
}

export function isRotorError(x: unknown): x is RotorError {
  return x instanceof RotorError;
}

function causeString(cause: unknown): string {
  if (cause instanceof Error) return `${cause.name}: ${cause.message}`;
  return typeof cause === "string" ? cause : JSON.stringify(cause);
}
