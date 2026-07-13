/**
 * The seven capability seams (docs/runtime.md §3). A runtime is the run loop
 * (§2.2) plus these seven pluggable capabilities; each is a stable interface,
 * the reference runtime ships a BASIC implementation, and glyphh drops a PREMIUM
 * one behind the same interface. Every capability is a {@link Capability}
 * (uniform `status()` that degrades rather than raising).
 *
 * These are the in-code contracts the executor and handlers program against.
 * They mirror the interface sketches in docs/runtime.md §3.1–§3.7.
 */

import type { Capability } from "../runtime/registry.js";
import type {
  Frame,
  Principal,
  RotorDocument,
  Step,
  StepRecord,
  Usage,
} from "../types.js";
import type { QueryResult, Row } from "../exec/store.js";

export type { QueryResult, Row } from "../exec/store.js";

// ── §3.1 grounding / encoder ────────────────────────────────────────────────

export interface ProbeResult {
  filler: string | null;
  membership: number;
  margin: number;
  top: string[];
}

export interface GroundVerdict {
  grounded: boolean;
  membership: number;
  margin: number;
  top: string[];
}

export interface EncodeResult {
  cortex: number[];
  slots: Array<[string, string]>;
  dropped: Array<[string, string]>;
}

export interface GroundingPlugin extends Capability {
  /** `space_id = sha256(vector_dim, encoder_seed, roles_config)` (§15.4). */
  computeSpaceId(vectorDim: number, encoderSeed: number, rolesConfig: string): string;
  /** Refuse a cross-space bind (§15.4). Basic tier: records the bound space. */
  assertSpace(spaceId: string): void;
  /** NL fact → cortex (the `hdc.map` bridge, §7.3). */
  encode(roleFillers: Record<string, string>, spaceId?: string): EncodeResult;
  /** Entity-keyed probe (§7.6). */
  probe(entity: string, role: string, spaceId?: string): ProbeResult;
  /** The ground verdict (§6.3): grounded iff the winner matches `filler`. */
  verify(entity: string, role: string, filler: string, margin: number, spaceId?: string): GroundVerdict;
  /** The hard-gate mask source — the grounded continuations for `(entity, role)`. */
  groundedFillers(entity: string, role: string, spaceId?: string): string[];
}

// ── §3.2 memory / stator ────────────────────────────────────────────────────

export interface SemanticHit {
  text: string;
  score: number;
}

export interface MemoryPlugin extends Capability {
  /** The closed op set (§7.5); NO model-generated SQL. */
  executeOp(op: string, params: Row, spaceId?: string): QueryResult;
  /** Embed + rank over recorded turns (§7.7); basic tier is deterministic-local
   *  cosine over the hashed-ngram embedding. */
  semanticRecall(query: string, topK: number, threshold: number): SemanticHit[];
  /** Record a turn into short-term memory (inbound prompt / outbound completion),
   *  the corpus `semanticRecall` ranks over. */
  recordTurn(text: string): void;
  /** Persist facts (§7.4). Idempotent by `key`. Returns the count written. */
  write(
    facts: Array<Record<string, unknown>>,
    opts: { key?: string; mode?: string; speaker?: string; spaceId?: string; tick?: number },
  ): number;
  probe(entity: string, role: string, spaceId?: string): ProbeResult;
  verify(entity: string, role: string, filler: string, margin: number, spaceId?: string): GroundVerdict;

  // event history — the source of truth (§5.4)
  appendStepRecord(rec: StepRecord): void;
  readEventHistory(runId: string): StepRecord[];
  lookupRecord(runId: string, stepId: string, attempt: number): StepRecord | undefined;
  lastAttempt(runId: string, stepId: string): number;

  // result cache (§5.7) — key = idempotency key; a hit is checkpointed once
  cacheGet(key: string, tick: number): Row | undefined;
  cachePut(key: string, output: Row, opts: { ttlTicks?: number; scope?: string }): void;

  /** short → mid → long consolidation (§7.18) over the `span`-recent window. */
  cascade(span?: number): { short: number; mid: number; long: number };
}

// ── §3.3 models / inference lanes ───────────────────────────────────────────

export interface ModelResult {
  text: string;
  frames: Frame[];
  usage: Usage;
}

export interface ModelRequest {
  prompt: string;
  /** Grounded continuations to rank when there is no live model (the zero-model
   *  ranker degrades to echoing a grounded filler, §3.3). */
  candidates?: string[];
  lane?: string;
  model?: string;
  temperature?: number;
  seed?: number;
}

export type LadderRung = "local" | "frontier" | "human";

export interface ModelsPlugin extends Capability {
  /** Route local (free) vs frontier (metered) — basic tier is local-only. */
  decide(lane: string | undefined): { lane: "local" | "frontier" };
  /** The quarantined data-plane call (§7.2). Tokens recorded, never reproduced. */
  execute(request: ModelRequest, lane: string): Promise<ModelResult>;
  /** Nearest-prototype router with an OUT_OF_SCHEMA reject class (§7.11 note). */
  classify(question: string, classes: string[]): { class: string; margin: number };
  /** The escalation ladder rung selection (§7.15, §9). */
  escalate(trigger: string, to?: string, fallback?: string): { lane: LadderRung };
}

// ── §3.4 connections / tools ────────────────────────────────────────────────

export type ToolHandler = (args: Row) => unknown | Promise<unknown>;

export interface ToolSchema {
  name: string;
  description?: string;
  inputSchema?: unknown;
}

export type DispatchResult = { ok: true; result: unknown } | { ok: false; error: string };

export interface ConnectionsPlugin extends Capability {
  register(method: string, handler: ToolHandler, schema?: ToolSchema): void;
  listTools(): ToolSchema[];
  /** NEVER raises → its failure becomes a frame (§3.4). */
  dispatch(method: string, args: Row): Promise<DispatchResult>;
  /** Raises on error (for callers that want to catch). */
  invoke(method: string, args: Row): Promise<unknown>;
}

// ── §3.5 gateway adapters ───────────────────────────────────────────────────

export interface MeterSnapshot {
  calls: number;
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
  cost: number;
}

/** A prompt-cache accounting result (§8.6) — determinism-neutral usage only. */
export interface PromptCacheResult {
  disposition: "write" | "hit";
  cache_read: number;
  cache_write: number;
}

export interface GatewayPlugin extends Capability {
  inAdapter(wire: unknown): unknown;
  outAdapter(io: unknown): unknown;
  /** Table-driven; a missing entry is `E_UNTRANSLATABLE`, never a silent drop. */
  translate(from: string, to: string, payload: unknown): unknown;
  /** Lower abstract prompt-cache breakpoints to a provider's wire form (§8.6).
   *  Unhonorable → no-op (`undefined`), never error. */
  lowerPromptCache(breakpoints: string[] | undefined, provider: string): unknown;
  /** Account a prompt-prefix cache lookup (§8.6): first sight of a prefix key is a
   *  write, later sights are hits. Determinism-neutral — usage only. */
  accountPromptCache(prefixKey: string, promptTokens: number): PromptCacheResult;
  /** Accumulate metered usage (§8.5). Local is unmetered by construction. */
  recordUsage(usage: Usage, lane?: string): void;
  meter(): MeterSnapshot;
}

// ── §3.6 governance / metering ──────────────────────────────────────────────

export interface GrantSet {
  /** Allowed step types; `undefined` = open (bare-box default, §3.6). */
  stepTypes?: Set<string>;
  tools?: Set<string>;
  models?: Set<string>;
  scopes: Set<string>;
  /** Field names redacted before entering Context (§13.4). */
  redactFields?: Set<string>;
  principal?: Principal;
}

export type PolicyVerdict = "allow" | "deny";

export interface GovernancePlugin extends Capability {
  /** Checkpointed once at run start (§3.6 enforcement point 1). */
  resolveGrants(doc: RotorDocument, principal?: Principal): GrantSet;
  /** Enforcement point 2 — before a step runs. */
  requirePolicy(step: Step, grants: GrantSet): PolicyVerdict;
  /** Enforcement point 3 — redact ungranted fields before Context. */
  redact(input: Row, grants: GrantSet): Row;
  /** attention ⊂ rotor ⊂ org budget (§13.2). Basic: never blocks. */
  checkBudget(): "ok" | "budget-exceeded";
  recordUsage(usage: Usage): void;
}

// ── §3.7 pool / affinity ────────────────────────────────────────────────────

export type InstanceState = "hot" | "warm" | "cold";

export interface PoolPlugin extends Capability {
  provision(): void;
  route(): string;
  /** Telemetry ONLY — MUST NOT influence a transition (§17.6). */
  instanceState(id?: string): InstanceState;
}

// ── §3.8 drain / observability sink ─────────────────────────────────────────

/**
 * The wire shape a log drain forwards — one per `StepRecord` (CloudEvents-ish,
 * per the §14 aspiration). It carries the record's audit + telemetry fields; the
 * `output` may be field-redacted before it leaves the process.
 */
export interface DrainEnvelope {
  /** Event type, e.g. `com.openrotor.step.v0`. */
  type: string;
  run_id: string;
  step_id: string;
  attempt: number;
  logical_tick: number;
  status: string;
  space_id?: string;
  principal?: { id: string; kind: string; scopes?: string[] };
  agent?: { ref: string; run_id: string };
  usage?: Usage;
  frames?: Frame[];
  output?: Record<string, unknown>;
  error?: { name: string; cause?: string };
}

/**
 * A log drain streams the run's `StepRecord` event stream to an external sink for
 * audit / FinOps / observability. It is strictly a telemetry **observer** — it
 * MUST NOT influence control flow ("telemetry only, never a control predicate",
 * SPEC.md §17.6), and `emit` MUST be fire-and-forget: it never blocks the run and
 * never throws. Delivery is best-effort and asynchronous.
 */
export interface DrainPlugin extends Capability {
  /** Enqueue a record for delivery. Fire-and-forget; never throws. */
  emit(rec: StepRecord): void;
  /** Flush buffered envelopes to the sink. */
  flush(): Promise<void>;
  /** Flush and release resources (called on shutdown). */
  close(): Promise<void>;
}

// ── the bundle ──────────────────────────────────────────────────────────────

/** The eight capabilities the executor holds (docs/runtime.md §2.1, §3.8). */
export interface Plugins {
  grounding: GroundingPlugin;
  memory: MemoryPlugin;
  models: ModelsPlugin;
  connections: ConnectionsPlugin;
  gateway: GatewayPlugin;
  governance: GovernancePlugin;
  pool: PoolPlugin;
  drain: DrainPlugin;
}
