/**
 * RotorSpec core types — the faithful TypeScript shapes for a Rotor Document,
 * its steps, the run-time StepRecord / Frame stream, and the run Context.
 *
 * These mirror SPEC.md §4 (Rotor Document), §4.2 (step envelope), §7 (the 20
 * step types), §5.4 (StepRecord), and §2 (Frame). The JSON Schema under
 * spec/schema/ is the *normative* validator; this module is the in-code contract
 * downstream handlers, the engine, and the CLI program against. Field names
 * match SPEC.md and rotor.schema.json 1:1.
 *
 * Spec version: rotor.glyphh.ai/v0.1
 */

import type { CapabilityStatus } from "./runtime/registry.js";
import type { MemoryTier } from "./exec/facts.js";

export type { CapabilityStatus } from "./runtime/registry.js";

// ───────────────────────────────────────────────────────────────────────────
// §7 — The closed step catalog (exactly 20 types).
// ───────────────────────────────────────────────────────────────────────────

/** The closed catalog of step types (SPEC.md §7). An engine MUST reject any
 *  `type` outside this set at Core (L1). */
export const STEP_TYPES = [
  "prompt", // §7.1  compose a bounded prompt (no model call)
  "model", // §7.2  a model call (the quarantined stochastic Task)
  "hdc.map", // §7.3  map NL → sim (the grounding bridge)
  "write", // §7.4  persist a fact (WRITE macro step)
  "retrieve.sql", // §7.5  deterministic closed-op query (RECALL)
  "retrieve.kb", // §7.6  entity-keyed associative / graph recall
  "gate", // §7.8  accept / reject / escalate
  "assert", // §7.9  grounded terminal / refuse
  "plan", // §7.10 typed constrained decode + deterministic execute
  "branch", // §7.11 deterministic router (Choice)
  "loop", // §7.12 bounded evaluator-optimizer
  "parallel", // §7.13 fan-out + reducer fan-in (Map / Parallel)
  "wait", // §7.14 pause, checkpoint, resume (a.k.a. interrupt)
  "escalate", // §7.15 local → frontier → human (the ladder)
  "tool", // §7.16 deterministic MCP tool / side-effecting app method
  "transform", // §7.17 pure Pass
  "sub-rotor", // §7.19 callable rotor / handoff (composition)
  "fail", // §7.20 typed terminal failure
] as const;

export type StepType = (typeof STEP_TYPES)[number];

/** Reserved terminal targets a `next` / branch / gate edge may name (SPEC.md
 *  §4.2). `end` = succeed, `__fail__` = typed failure. A refusing `assert` is
 *  also a terminal but is a step id, not a reserved word. */
export const RESERVED_TERMINALS = ["end", "__fail__"] as const;
export type ReservedTerminal = (typeof RESERVED_TERMINALS)[number];

/** A `next` target: another step's id or a reserved terminal. */
export type NextTarget = string;

// ───────────────────────────────────────────────────────────────────────────
// §4 — Rotor Document top-level.
// ───────────────────────────────────────────────────────────────────────────

/** The four-field top-level document (SPEC.md §4.1). */
export interface RotorDocument {
  /** `rotor.glyphh.ai/vMAJOR.MINOR` — selects spec semantics. */
  apiVersion: string;
  /** `Rotor` (this spec) or `SubRotor` (a callable fragment, §15.3). */
  kind: "Rotor" | "SubRotor";
  metadata: RotorMetadata;
  spec: RotorSpecBody;
}

export interface RotorMetadata {
  /** Rotor identity within its namespace. */
  name: string;
  /** Semver of the *document* (§16). Runs pin to it. */
  version: string;
  namespace?: string;
  description?: string;
  labels?: Record<string, string>;
}

export interface RotorSpecBody {
  /** Typed run parameters; missing required inputs fail before step 1. */
  inputs: InputParam[];
  /** HDC space identity (§15.4). REQUIRED if any retrieval/gate/encode step. */
  space?: SpaceConfig;
  /** Transport + governance for all I/O (§8). */
  gateway?: GatewayConfig;
  /** Run-wide attention: budget + signal weights (§10). */
  attention?: AttentionConfig;
  /** DECLARES the required principal kind + scopes (§11). */
  identity?: IdentityConfig;
  /** DECLARES the control-plane surface the rotor uses (§13.1). */
  policy?: PolicyConfig;
  /** DECLARES the data-access the rotor requires; deny-by-default (§13.4). */
  access?: AccessConfig;
  /** Effectiveness telemetry + input/output anomaly gates (§14). */
  assurance?: AssuranceConfig;
  /** Wire-event verbosity: how much of each step's output streams to clients.
   *  Default `full` — a rotor SHOWS ITS WORK unless the author dials it down. */
  events?: { output?: EventOutputMode };
  /** Document-level default cross-run result-cache policy (§5.7). */
  cache?: CacheConfig;
  /** Prior `metadata.version`s this document is replay-compatible with (§16.3
   *  patch gates). A run recorded on a listed version replays cleanly against this
   *  edited document; an unlisted version fails with `E_REPLAY_DIVERGENCE`. */
  patch?: string[];
  /** Warm-pool intent — runtime-optional (§17.3). */
  pool?: PoolConfig;
  /** Warm-instance routing preference (§17.4). */
  affinity?: AffinityConfig;
  /** Typed shared-state schema + reducers (§5.2). */
  state?: StateConfig;
  /** Explicit entry step id; defaults to the first step (§5.1). */
  entry?: string;
  /** The transition graph — a non-empty ordered list of steps (§5.1, §7). */
  steps: Step[];
  /** Named projections from the final Context. */
  outputs?: OutputProjection[];
}

// ── spec.inputs / spec.outputs ─────────────────────────────────────────────

export type ValueType =
  | "string"
  | "number"
  | "boolean"
  | "object"
  | "array"
  | "vector";

export interface InputParam {
  name: string;
  type: ValueType;
  required?: boolean;
  default?: unknown;
  description?: string;
}

export interface OutputProjection {
  name: string;
  /** A Context reference, e.g. `$.state.answer`. */
  from: string;
}

// ── spec.space (§15.4) ─────────────────────────────────────────────────────

export interface SpaceConfig {
  vector_dim: number;
  encoder_seed: number;
  roles_config: string;
  /** Derived `sha256(vector_dim, encoder_seed, roles_config)`; MAY be pinned. */
  space_id?: string;
}

// ── spec.gateway (§8) ──────────────────────────────────────────────────────

export type RateLimitScope = "provider" | "rotor" | "step" | "tenant";
export type RateLimitOnExceed = "queue" | "backoff" | "decline";

export interface RateLimitRule {
  scope: RateLimitScope;
  key?: string;
  rpm?: number;
  tpm?: number;
  concurrency?: number;
  on_exceed?: RateLimitOnExceed;
}

export type MeterMode = "frontier-only" | "all" | "none";

export interface MeteringConfig {
  meter?: MeterMode;
  local?: "unmetered" | "metered";
  [k: string]: unknown;
}

export interface GatewayConfig {
  rateLimit?: RateLimitRule[];
  metering?: MeteringConfig;
  [k: string]: unknown;
}

// ── spec.attention (§10) ───────────────────────────────────────────────────

export type AttentionOnExhausted = "stop" | "escalate" | "best-effort";

export interface AttentionBudget {
  /** Wall-clock ms bound. */
  wall_ms?: number;
  /** Max revolutions of the loop. */
  revolutions?: number;
  tokens?: number;
  cost?: number;
  on_exhausted?: AttentionOnExhausted;
}

export interface AttentionConfig {
  budget?: AttentionBudget;
  /** Context-ref → weight; biases routing over the stator, adds no edges. */
  weights?: Record<string, number>;
}

// ── spec.identity (§11) ────────────────────────────────────────────────────

export type PrincipalKind = "user" | "service";
export type OnMissingScope = "refuse" | "escalate";

export interface IdentityConfig {
  principal?: {
    required?: boolean;
    kind?: PrincipalKind[];
  };
  /** Scopes the rotor needs, e.g. `memory:read:relational`. */
  scopes?: string[];
  on_missing_scope?: OnMissingScope;
}

// ── spec.policy (§13.1) ────────────────────────────────────────────────────

export interface PolicyConfig {
  /** The control-plane surface the rotor requires. */
  requires?: {
    step_types?: StepType[];
    tools?: string[];
    models?: string[];
    connections?: string[];
  };
  /** Approval gates the rotor expects (by step id). */
  approvals?: string[];
}

// ── spec.access (§13.4) ────────────────────────────────────────────────────

export type OnUngranted = "redact" | "refuse";

export interface StatorAccessScope {
  spaces?: string[];
  entities?: string[];
  roles?: string[];
}

export interface StatorAccess {
  read?: StatorAccessScope;
  write?: StatorAccessScope;
}

export interface ConnectionAccessScope {
  connector: string;
  operations?: string[];
  fields?: string[];
}

export interface AccessConfig {
  stator?: StatorAccess;
  connections?: ConnectionAccessScope[];
  on_ungranted?: OnUngranted;
  /** Field names redacted from step inputs AND outputs before they enter the
   *  Context / event history (§13.4). Deny-by-default at the field grain. */
  redact?: string[];
}

// ── spec.assurance (§14) ───────────────────────────────────────────────────

export type AnomalyMode = "firewall" | "anomaly" | "hdc-ground" | "off";
export type OnUngrounded = "refuse" | "escalate";

export interface AnomalyGateConfig {
  mode?: AnomalyMode;
  checks?: Array<"policy" | "pii" | "drift" | "ood">;
  scanner?: string;
  on_ungrounded?: OnUngrounded;
  [k: string]: unknown;
}

export interface AssuranceConfig {
  effectiveness?: {
    metrics?: string[];
  };
  anomaly?: {
    input?: AnomalyGateConfig;
    output?: AnomalyGateConfig;
  };
}

// ── spec.cache / envelope cache (§5.7) ─────────────────────────────────────

export type CacheScope = "run" | "rotor" | "tenant" | "global";

/** Cross-run result-cache policy. `cache: "none"` on a step disables it. */
export interface CacheConfig {
  /** `auto` reuses the §5.6 idempotency key; or a caller-controlled expr. */
  key?: "auto" | string;
  /** A duration (e.g. `1h`, `24h`) evaluated on the logical clock. */
  ttl?: string;
  scope?: CacheScope;
}

/** A step's envelope `cache` is either a policy block or the literal `"none"`. */
export type StepCache = CacheConfig | "none";

// ── spec.pool (§17.3) ──────────────────────────────────────────────────────

export interface PoolConfig {
  minHot?: number;
  maxHot?: number;
  targetConcurrency?: number;
  warm?: unknown;
  coldStart?: unknown;
  [k: string]: unknown;
}

// ── spec.affinity (§17.4) ──────────────────────────────────────────────────

export type AffinityKey = "tenant" | "conversation" | "entity";
export type AffinityMode = "prefer" | "require";

export interface AffinityConfig {
  keys?: AffinityKey[];
  mode?: AffinityMode;
}

// ── spec.state (§5.2) ──────────────────────────────────────────────────────

export type Reducer =
  | "last-write-wins"
  | "append"
  | "merge"
  | "sum"
  | "max"
  | "min"
  | "union"
  | string; // a named sub-rotor reducer

export interface StateFieldSchema {
  type: ValueType;
  [k: string]: unknown;
}

export interface StateConfig {
  schema?: Record<string, StateFieldSchema>;
  reducers?: Record<string, Reducer>;
}

// ───────────────────────────────────────────────────────────────────────────
// §4.2 — The step envelope (common to every type; `config` is typed by `type`).
// ───────────────────────────────────────────────────────────────────────────

export type Idempotency = "auto" | "none" | string;

/** An ordered retrier (SPEC.md §5.5). */
export interface Retrier {
  /** Typed error names, matched in order. */
  errors: string[];
  interval_ms?: number;
  max_attempts?: number;
  backoff_rate?: number;
}

/** An ordered catcher (SPEC.md §5.5). */
export interface Catcher {
  /** Typed error names or `"*"`. */
  errors: string[];
  next: NextTarget;
}

/** Per-step overrides of the document-level attention/access/assurance/gateway. */
export interface StepAttention {
  budget?: AttentionBudget;
  weights?: Record<string, number>;
}

export interface StepAccess {
  /** A step MAY only *narrow* spec.access (§4.2, §13.4). */
  stator?: StatorAccess;
  connections?: ConnectionAccessScope[];
}

export interface StepAssurance {
  anomaly?: {
    input?: AnomalyGateConfig;
    output?: AnomalyGateConfig;
  };
}

export interface StepAffinity {
  keys?: AffinityKey[];
  mode?: AffinityMode;
}

/**
 * The common step envelope (SPEC.md §4.2). `config` is discriminated by `type`
 * via {@link StepConfigMap}; {@link Step} is the type-safe discriminated union.
 */
/** How much of a step's OUTPUT rides its wire event: `full` — the complete
 *  (already-redacted) output; `summary` — values bounded for narration;
 *  `min` — identity/status/usage only, no output. */
export type EventOutputMode = "full" | "summary" | "min";

/** Author-controlled display metadata for a step: streamed verbatim on the step's
 *  wire event so clients render the author's words, not raw step ids. */
export interface StepDisplay {
  /** Short human progress label, e.g. "planning the change". */
  label: string;
  /** Optional one-line detail a rich client can show on hover/expand. */
  detail?: string;
  /** Override the rotor's `spec.events.output` for THIS step — silence plumbing
   *  (`min`) or bound a bulky step (`summary`) while the rotor default stays full. */
  output?: EventOutputMode;
}

export interface StepBase {
  /** MUST be unique within the rotor. */
  id: string;
  type: StepType;
  /** Display metadata streamed to clients with this step's wire event. */
  display?: StepDisplay;
  /** Typed input signature: names → Context references (`$.inputs.*`, …). */
  in?: Record<string, string>;
  /** Typed output signature: names (with declared types) written to Context. */
  out?: Record<string, ValueType | string>;
  /** Default successor (id | reserved terminal). branch/loop/gate may override. */
  next?: NextTarget;
  /** Ordered retriers (§5.5). */
  retry?: Retrier[];
  /** Ordered catchers (§5.5). */
  catch?: Catcher[];
  /** Idempotency key policy (§5.6). */
  idempotency?: Idempotency;
  /** Cross-run result memoization (§5.7); `"none"` disables. */
  cache?: StepCache;
  attention?: StepAttention;
  access?: StepAccess;
  assurance?: StepAssurance;
  gateway?: GatewayConfig;
  affinity?: StepAffinity;
}

// ───────────────────────────────────────────────────────────────────────────
// §7 — Per-type `config` shapes.
// ───────────────────────────────────────────────────────────────────────────

/** Prompt-cache breakpoints (§8.6) carried on a `prompt`/`model` step. */
export interface PromptCacheConfig {
  breakpoints?: string[];
}

export interface PromptBlock {
  name: string;
  text: string;
}

/** §7.1 prompt */
export interface PromptConfig {
  template?: string;
  blocks?: PromptBlock[];
  max_tokens?: number;
  cache?: PromptCacheConfig;
}

export type ModelLane = "local" | "frontier";
export type Enforcement = "hard" | "soft";

/** Inline grounding gate on a `model` step (§7.2). */
export interface InlineGround {
  entity?: string;
  role?: string;
  enforcement?: Enforcement;
  refusal?: string;
}

/** §7.2 model */
export interface ModelConfig {
  lane?: ModelLane;
  model?: string;
  max_tokens?: number;
  /** Per-STEP model-call timeout (ms) — a transport liveness bound, declared
   *  where the workload is known: a router classifies in seconds, a coder
   *  emitting a large file needs minutes. Overrides ROTOR_MODEL_TIMEOUT. */
  timeout_ms?: number;
  temperature?: number;
  tools?: string[];
  /** Best-effort recorded seed; never a correctness guarantee (§6.3). */
  seed?: number;
  ground?: InlineGround;
  /** Run the word-level micro rotor. */
  micro?: boolean;
  max_backtracks?: number;
  cache?: PromptCacheConfig;
}

/** §7.3 hdc.map */
export interface HdcMapConfig {
  schema?: string;
  enricher?: "heuristic" | "local" | "auto";
}

/** §7.4 write */
export interface WriteConfig {
  /** `raw` verbatim fact · `absorb` NL→fact enricher · `turn` a conversational
   *  exchange: appended to the session's recency window AND the similarity
   *  corpus, speaker-tagged. */
  mode?: "raw" | "absorb" | "turn";
  key?: string;
  speaker?: string;
  /** Explicit retention tier for the facts this step writes (docs/memory.md):
   *  `long` lifelong, `mid` within a session window, `short` this session only.
   *  Deterministic — the spec author's own lever. Overrides the absorb enricher's
   *  per-fact default. Omit to let the enricher decide (directive/self → long). */
  tier?: MemoryTier;
}

export type SqlOp =
  | "lookup"
  | "prev"
  | "count"
  | "count_not"
  | "top"
  | "who"
  | "compare"
  | "refuse";

/** §7.5 retrieve.sql */
export interface RetrieveSqlConfig {
  op: SqlOp;
  slot?: string;
  person?: string;
  conditions?: Record<string, unknown>;
  value?: unknown;
  a?: unknown;
  b?: unknown;
  k?: number;
}

/** §7.6 retrieve.kb */
export interface RetrieveKbConfig {
  mode: "probe" | "verify" | "node" | "neighbors";
  entity?: string;
  role?: string;
  topn?: number;
  depth?: number;
  margin?: number;
}

export type GateMode =
  | "hdc-ground"
  | "schema"
  | "assertion"
  | "evaluator"
  | "approval"
  | "firewall"
  | "anomaly";

/** §7.8 gate */
export interface GateConfig {
  mode: GateMode;
  // hdc-ground
  entity?: string;
  role?: string;
  admit?: number;
  margin?: number;
  enforcement?: Enforcement;
  // schema
  schema?: unknown;
  // evaluator
  threshold?: number;
  scorer?: string;
  // firewall (INPUT anomaly, §14.2)
  scanner?: string;
  // anomaly (OUTPUT anomaly, §14.2)
  checks?: Array<"policy" | "pii" | "drift" | "ood">;
  // routing
  on_pass?: NextTarget;
  on_fail?: NextTarget;
  on_escalate?: NextTarget;
}

/** §7.9 assert */
export interface AssertConfig {
  refusal?: string;
  empty_cell?: "refuse" | "escalate";
}

/** §7.10 plan */
export interface PlanConfig {
  ops?: string[];
  executor?: string;
  on_out_of_schema?: "refuse";
  [k: string]: unknown;
}

/** §7.11 branch */
export interface BranchCase {
  /** A pure predicate over recorded state. */
  when: string;
  next: NextTarget;
}
export interface BranchConfig {
  cases: BranchCase[];
  default?: NextTarget;
}

/** §7.12 loop */
export interface LoopConfig {
  body: string;
  gate?: GateConfig;
  /** REQUIRED — bounds iteration; termination guaranteed. */
  max_iterations: number;
  budget?: AttentionBudget | Record<string, unknown>;
  on_exhausted?: "escalate" | "refuse" | "best-so-far";
}

/** §7.13 parallel */
export interface ParallelConfig {
  mode: "parallel" | "map";
  branches?: string[];
  over?: string;
  as?: string;
  body?: string;
  reducer?: Record<string, Reducer> | Reducer;
  max_concurrency?: number;
}

/** §7.14 wait / interrupt */
export interface WaitConfig {
  on: "approval" | "signal" | "timer" | "callback";
  token?: string;
  timeout_ms?: number;
  on_timeout?: "escalate" | "fail" | "resume";
}

/** §7.15 escalate */
export interface EscalateConfig {
  trigger:
    | "refuse"
    | "low-margin"
    | "frontier-decline"
    | "gate-reject"
    | "budget-exceeded";
  to?: "frontier" | "human";
  frontier_model?: string;
  gateway_url?: string;
  auth_token?: string;
  fallback?: "local" | "human" | "refuse";
}

/** §7.16 tool */
export interface ToolConfig {
  flavor: "mcp" | "app";
  name?: string;
  method?: string;
  args?: Record<string, unknown>;
  params?: Record<string, unknown>;
}

/** §7.17 transform */
export interface TransformConfig {
  set?: Record<string, unknown>;
  map?: Record<string, string>;
  /** Deterministic field extraction (§7.17): each entry is `name → regex` applied
   *  to the step's `in.text`; the FIRST capture group (trimmed) becomes the output
   *  field, or `null` when unmatched. Pure regex — no model, replay-safe. This is
   *  how a conversation-native rotor lifts structure out of a planner's text. */
  parse?: Record<string, string>;
  /** `fences`: strip a leading ```lang line and trailing ``` from `in.text`,
   *  emitting `text` — models fence code no matter what the brief says, and a
   *  written file must never contain markdown. Deterministic string surgery. */
  strip?: "fences";
}

/** §7.19 sub-rotor */
export interface SubRotorConfig {
  /** `namespace/name@version`. */
  ref: string;
  mode?: "call" | "handoff";
  inputs?: Record<string, string>;
  space?: "inherit" | SpaceConfig;
}

/** §7.20 fail */
export interface FailConfig {
  error: string;
  cause?: string;
}

/** Map each step type to its `config` shape. Steps whose config is optional
 *  (`prompt`, `transform`, `hdc.map`, `write`, …) allow it to be omitted. */
export interface StepConfigMap {
  prompt: PromptConfig;
  model: ModelConfig;
  "hdc.map": HdcMapConfig;
  write: WriteConfig;
  "retrieve.sql": RetrieveSqlConfig;
  "retrieve.kb": RetrieveKbConfig;
  gate: GateConfig;
  assert: AssertConfig;
  plan: PlanConfig;
  branch: BranchConfig;
  loop: LoopConfig;
  parallel: ParallelConfig;
  wait: WaitConfig;
  escalate: EscalateConfig;
  tool: ToolConfig;
  transform: TransformConfig;
  "sub-rotor": SubRotorConfig;
  fail: FailConfig;
}

/** A single step, discriminated on `type`, carrying the matching `config`. */
export type Step = {
  [K in StepType]: StepBase & { type: K; config?: StepConfigMap[K] };
}[StepType];

// ───────────────────────────────────────────────────────────────────────────
// §2 — Frames. A step's output contract is a *stream* of typed frames.
// ───────────────────────────────────────────────────────────────────────────

/** The closed set of frame kinds a step MAY emit (SPEC.md §2). */
export const FRAME_TYPES = [
  "propose",
  "stub",
  "degrade",
  "parse",
  "dispose",
  "backtrack",
  "gate",
  "assert",
  "refuse",
  "cache",
  "delta",
  "tool",
  "done",
] as const;

export type FrameType = (typeof FRAME_TYPES)[number];

/** A caching disposition recorded on a `cache` frame (§5.7 result cache /
 *  §8.6 prompt cache). */
export type CacheDisposition = "hit" | "miss" | "write";

/** A typed event emitted by a step as it executes (SPEC.md §2). */
export interface Frame {
  type: FrameType;
  /** The logical tick at which the frame was emitted (§5.3). */
  logical_tick?: number;
  /** Frame payload — shape varies by `type`. */
  data?: unknown;
  /** For `cache` frames: the recorded disposition. */
  disposition?: CacheDisposition;
}

// ───────────────────────────────────────────────────────────────────────────
// §5.4 — The StepRecord (checkpoint appended to the run's event history).
// ───────────────────────────────────────────────────────────────────────────

/** Terminal status of a step execution (SPEC.md §5.4). */
export type StepStatus =
  | "ok"
  | "refused"
  | "failed"
  | "escalated"
  | "interrupted";

/** Token/credit accounting for a metered call (§8.5); broken out by cache
 *  disposition because each is priced differently. */
export interface Usage {
  input?: number;
  output?: number;
  cache_write?: number;
  cache_read?: number;
  cost?: number;
  [k: string]: number | undefined;
}

/** A typed error name + cause carried by a failed StepRecord (§5.4, §5.5). */
export interface StepError {
  /** Typed error name, e.g. `E_TRANSPORT`, `FrontierDeclined`, `E_UNMERGEABLE`. */
  name: string;
  cause?: string;
}

/**
 * The append-only checkpoint written after each step (SPEC.md §5.4). The event
 * history of `StepRecord`s is the single source of truth for replay: on replay,
 * a record for `(step_id, attempt)` returns its `output` as-is — the model/tool
 * is never re-invoked.
 */
export interface StepRecord {
  run_id: string;
  step_id: string;
  /** Retry attempt number (0-based). */
  attempt: number;
  /** Monotonic logical step counter (§5.3) — never wall-clock. */
  logical_tick: number;
  /** `sha256` of the canonicalized `in`. */
  input_hash: string;
  /** `sha256(definitionVersion, step_id, canonical(in), space_id)` (§5.6). */
  idempotency_key: string;
  /** The HDC space bound against (§15.4), where applicable. */
  space_id?: string;
  /** The document version this record was produced under (§16.3 run-pinning). */
  definitionVersion?: string;
  /** The authenticated caller (§11) — audit trail. */
  principal?: Principal;
  /** The rotor instance (§11) — attribution. */
  agent_identity?: AgentIdentity;
  status: StepStatus;
  /** The `out` values written to Context. */
  output?: Record<string, unknown>;
  /** The emitted frame stream. */
  frames?: Frame[];
  /** Metered usage recorded for this step (§8.5). */
  usage?: Usage;
  /** The step's author display metadata, copied onto the record for clients. */
  display?: StepDisplay;
  /** The step's effective wire-output mode (display.output ?? spec.events.output). */
  event_output?: EventOutputMode;
  /** Typed error name + cause, present iff `status === "failed"`. */
  error?: StepError;
}

// ───────────────────────────────────────────────────────────────────────────
// §11 — Identities recorded in every StepRecord.
// ───────────────────────────────────────────────────────────────────────────

/** The authenticated caller a run executes under (SPEC.md §11). */
export interface Principal {
  id: string;
  kind: PrincipalKind;
  /** The granted scope set, checkpointed at run start. */
  scopes?: string[];
}

/** The rotor instance — the unit of attribution, derived from
 *  `(namespace/name@version, run_id)` (SPEC.md §11). */
export interface AgentIdentity {
  /** `namespace/name@version`. */
  ref: string;
  run_id: string;
}

// ───────────────────────────────────────────────────────────────────────────
// §5.2 / runtime.md — The run Context and per-run execution state.
// ───────────────────────────────────────────────────────────────────────────

/**
 * The typed shared state that flows through a run (SPEC.md §5.2): declared
 * `state` plus namespaced step outputs. Steps read via `in` references
 * (`$.inputs.*`, `$.state.*`, `$.steps.<id>.<out>`) and write via `out` names.
 */
export interface RunContext {
  /** Concrete run inputs (validated against spec.inputs). */
  inputs: Record<string, unknown>;
  /** The typed shared state (spec.state.schema), merged via reducers. */
  state: Record<string, unknown>;
  /** Namespaced per-step outputs: `steps[stepId][outName]`. */
  steps: Record<string, Record<string, unknown>>;
}

/**
 * The ambient per-run execution context handed to a step handler
 * (runtime.md §2.3, §4.3): `handler.execute(step, input, grants, ctx)`.
 */
export interface RunContextEnvelope {
  run_id: string;
  /** The session this run belongs to (docs/memory.md retention tiers). Scopes
   *  short/mid-tier fact writes; `undefined` ⇒ session-agnostic (permissive
   *  recall, backward compatible). One runtime engagement = one session. */
  session?: string;
  /** The document version this run is pinned to (§16.3). */
  definitionVersion: string;
  /** Monotonic logical step counter (§5.3). */
  logical_tick: number;
  space_id?: string;
  principal?: Principal;
  agent_identity?: AgentIdentity;
  /** The live Context (§5.2). */
  context: RunContext;
  /** Advertised capability manifest for negotiation (§3.8). */
  capabilities?: Record<string, CapabilityStatus>;
  /** The caller's cancellation signal (a user Esc-interrupt). The run loop checks
   *  it between steps; the `model` step forwards it to the provider fetch so a
   *  long in-flight call is cut, not just the loop stopped. */
  signal?: AbortSignal;
}

/** What a step handler returns: its `out` values plus a frame stream
 *  (runtime.md §2.3, §4.3). The engine records this, then merges + routes. */
export interface StepResult {
  output: Record<string, unknown>;
  frames: Frame[];
  status?: StepStatus;
  usage?: Usage;
  error?: StepError;
}
