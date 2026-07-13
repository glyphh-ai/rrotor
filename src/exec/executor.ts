/**
 * The executor — the run loop from SPEC.md §5 and docs/runtime.md §2.2.
 *
 *   authenticate → resolve+checkpoint grants
 *   cursor := entry
 *   while cursor not terminal:
 *     step := steps[cursor]
 *     require_policy(step, grants)                    ── trust point 2
 *     input := redact(resolve(step.in, Context))      ── trust point 3
 *     if history has record(step_id, attempt): REPLAY (no execute)
 *     else: cache_get | execute(step) → append StepRecord
 *     merge(output → Context, reducers)
 *     cursor := select_next(step, output, Context)    ── CONTROL plane
 *     logical_tick++
 *   project spec.outputs
 *
 * The only nondeterministic line is `execute()`; everything else is a pure
 * function of recorded state (§6). Control flow (`select_next`) never reads
 * wall-clock, RNG, or unordered iteration — maps iterate by sorted key.
 *
 * Checkpoint + replay (§5.4): a fresh `execute()` against a stator that already
 * holds this run's history returns recorded outputs step-for-step and never
 * re-invokes a handler for a recordless-free step. Composite steps (`loop`,
 * `parallel`, `sub-rotor`) always re-run their handler so their children replay
 * individually; leaf steps short-circuit from their own record.
 */

import type {
  Catcher,
  Frame,
  GateConfig,
  LoopConfig,
  Principal,
  Retrier,
  RotorDocument,
  RunContext,
  RunContextEnvelope,
  Step,
  StepRecord,
  StepResult,
  StepStatus,
  StepType,
} from "../types.js";
import {
  applyReducer,
  canonicalize,
  idempotencyKey,
  parseTicks,
  resolveInputs,
  resolveRef,
  scopedCacheKey,
  sha256,
} from "./util.js";
import { evalPredicate } from "./util.js";
import type { Plugins } from "../plugins/interfaces.js";
import type { CacheConfig, CacheScope } from "../types.js";
import {
  buildHandlers,
  type Engine,
  type StepHandler,
} from "../handlers/index.js";
import { AttentionMeter, type BudgetOutcome } from "./budget.js";
import { RotorError } from "../errors.js";

// ───────────────────────────────────────────────────────────────────────────
// Public surface.
// ───────────────────────────────────────────────────────────────────────────

export interface ExecuteOptions {
  /** Pin a run id; defaults to a content-address of the doc + inputs. */
  runId?: string;
  /** The session this run belongs to (docs/memory.md retention tiers). Stamps
   *  short/mid-tier fact writes so they scope correctly across sessions. Omit for
   *  a session-agnostic run (permissive recall). */
  session?: string;
  /** The authenticated caller (§11); defaults to a local principal. */
  principal?: Principal;
  /** Override / extend the dispatch table (e.g. inject a premium handler). */
  handlers?: Partial<Record<StepType, StepHandler>>;
  /** Resolve a `sub-rotor` `ref` → its document (§7.19). */
  rotorResolver?: (ref: string) => RotorDocument | undefined;
  /** Safety cap on total physical step executions. */
  maxTicks?: number;
  /** Sleep between retry attempts (§5.5 backoff). Injectable for tests; defaults
   *  to a real timer. Only used on FRESH execution — replay never sleeps. */
  sleep?: (ms: number) => Promise<void>;
  /** Resume a previously interrupted run (§7.14): re-execute the same run id,
   *  injecting `payload` into the interrupted step so it completes instead of
   *  pausing again. `timeout` routes the step via its `on_timeout`. */
  resume?: { stepId: string; payload?: Record<string, unknown>; timeout?: boolean };
}

export interface RunResult {
  run_id: string;
  status: StepStatus;
  /** The terminal reached: `end` | `__fail__` | `__budget__` | a terminal step id. */
  terminal: string;
  /** Projected `spec.outputs` from the final Context. */
  outputs: Record<string, unknown>;
  /** The final run Context. */
  context: RunContext;
  /** The append-only event history for this run (§5.4). */
  history: StepRecord[];
  /** Set when an attention budget (§10) was exhausted — deterministic. */
  budget?: BudgetOutcome;
  /** Set when the run paused at a `wait`/`approval` step (§7.14). Resume by
   *  re-executing with `ExecuteOptions.resume = { stepId, payload }`. */
  interrupt?: { stepId: string; awaiting?: unknown };
  /** The typed error that drove the run to a `__fail__` terminal, if any. */
  error?: { name: string; cause?: string };
}

const COMPOSITE: ReadonlySet<StepType> = new Set<StepType>(["loop", "parallel", "sub-rotor"]);
const CACHEABLE_DEFAULT: ReadonlySet<StepType> = new Set<StepType>([
  "retrieve.sql",
  "retrieve.kb",
  "retrieve.vector",
  "hdc.map",
  "transform",
  "tool",
]);

/** Execute a rotor document against concrete inputs with the given plugins. */
export async function execute(
  doc: RotorDocument,
  inputs: Record<string, unknown>,
  plugins: Plugins,
  opts: ExecuteOptions = {},
): Promise<RunResult> {
  return new RunSession(doc, inputs, plugins, opts).run();
}

// ───────────────────────────────────────────────────────────────────────────
// The per-run session.
// ───────────────────────────────────────────────────────────────────────────

class RunSession {
  private readonly handlers: Map<StepType, StepHandler>;
  private readonly stepsById = new Map<string, Step>();
  private readonly reducers: Record<string, string>;
  private readonly stateKeys: Set<string>;
  private readonly runId: string;
  private readonly definitionVersion: string;
  private readonly spaceId?: string;
  private readonly principal: Principal;
  private readonly agentRef: string;
  private readonly maxTicks: number;
  private readonly env: RunContextEnvelope;
  private readonly engine: Engine;
  /** Session-local per-step visit counter — the replay key (§5.4). */
  private readonly visits = new Map<string, number>();
  private lastStatus: StepStatus = "ok";
  /** Deterministic §10 attention budget meter. */
  private readonly attention: AttentionMeter;
  private readonly sleep: (ms: number) => Promise<void>;
  private budgetOutcome?: BudgetOutcome;
  private budgetHandled = false;
  private interrupt?: { stepId: string; awaiting?: unknown };
  private lastError?: { name: string; cause?: string };

  constructor(
    private readonly doc: RotorDocument,
    inputs: Record<string, unknown>,
    private readonly plugins: Plugins,
    private readonly opts: ExecuteOptions,
  ) {
    this.handlers = buildHandlers(opts.handlers);
    for (const s of doc.spec.steps) this.stepsById.set(s.id, s);
    this.reducers = doc.spec.state?.reducers ?? {};
    this.stateKeys = new Set(Object.keys(doc.spec.state?.schema ?? {}));
    this.definitionVersion = doc.metadata.version;
    this.maxTicks = opts.maxTicks ?? 10_000;
    this.attention = new AttentionMeter(doc.spec.attention?.budget);
    this.sleep = opts.sleep ?? ((ms) => new Promise((r) => setTimeout(r, ms)));

    // Space identity (§15.4): compute + bind if the document declares a space.
    if (doc.spec.space) {
      const { vector_dim, encoder_seed, roles_config, space_id } = doc.spec.space;
      this.spaceId = space_id ?? plugins.grounding.computeSpaceId(vector_dim, encoder_seed, roles_config);
      plugins.grounding.assertSpace(this.spaceId);
    }

    // Resolve concrete inputs (validate required, fill defaults) before step 1.
    const resolvedInputs = validateInputs(doc, inputs);

    // Agent identity (§11): namespace/name@version. Many definitions share one
    // stator, so the run id MUST content-address the ROTOR IDENTITY, not just its
    // version — otherwise base@0.1.0 and super-power@0.1.0 with the same inputs
    // would collide on run_id and mix their run tapes.
    const ns = doc.metadata.namespace ? doc.metadata.namespace + "/" : "";
    this.agentRef = `${ns}${doc.metadata.name}@${doc.metadata.version}`;

    this.runId =
      opts.runId ?? "run-" + sha256(this.agentRef, " ", canonicalize(resolvedInputs)).slice(0, 16);

    // Identities (§11): checkpointed grant set + principal.
    const grants = plugins.governance.resolveGrants(doc, opts.principal);
    this.principal = grants.principal ?? opts.principal ?? { id: "local", kind: "user" };

    const context: RunContext = { inputs: resolvedInputs, state: {}, steps: {} };
    this.env = {
      run_id: this.runId,
      session: opts.session,
      definitionVersion: this.definitionVersion,
      logical_tick: 0,
      space_id: this.spaceId,
      principal: this.principal,
      agent_identity: { ref: this.agentRef, run_id: this.runId },
      context,
      capabilities: manifest(plugins),
    };

    this.engine = {
      env: this.env,
      runStep: async (stepId) => (await this.runStepById(stepId)).result,
      runRotor: (ref, subInputs) => this.runRotor(ref, subInputs),
      resolve: (ref, ctx) => resolveRef(ref, ctx ?? this.env.context),
    };
    // Stash grants for policy checks.
    this.grants = grants;
  }

  private grants: ReturnType<Plugins["governance"]["resolveGrants"]>;

  async run(): Promise<RunResult> {
    let cursor = this.doc.spec.entry ?? this.doc.spec.steps[0]?.id ?? "end";
    let terminal = "end";
    let runStatus: StepStatus = "ok";

    for (let guard = 0; ; guard++) {
      if (cursor === "end") {
        terminal = "end";
        runStatus = this.lastStatus === "refused" ? "refused" : "ok";
        break;
      }
      if (cursor === "__fail__") {
        terminal = "__fail__";
        runStatus = "failed";
        break;
      }
      if (guard > this.maxTicks) {
        terminal = "__fail__";
        runStatus = "failed";
        break;
      }
      // Trust point (§13.2) — org/tenant budget cap. Basic tier never blocks;
      // the seam is wired so a premium governance plugin can enforce it.
      if (this.plugins.governance.checkBudget() === "budget-exceeded") {
        this.budgetOutcome = { on: "stop", reason: "cost", ...this.attention.snapshot() };
        terminal = "__budget__";
        runStatus = "refused";
        break;
      }

      // §10 attention budget — deterministic (revolutions / tokens / cost).
      const exhausted = this.attention.exhausted();
      if (exhausted && !this.budgetHandled) {
        this.budgetHandled = true;
        this.budgetOutcome = exhausted;
        if (exhausted.on === "escalate") {
          const esc = this.findEscalateStep(cursor);
          if (esc) {
            cursor = esc; // raise the budget-exceeded escalation trigger (§9.1)
            continue;
          }
        }
        // stop | best-effort | escalate-with-no-ladder: end with the best so far.
        terminal = "__budget__";
        runStatus = exhausted.on === "best-effort" || exhausted.on === "stop" ? this.lastStatus : "refused";
        break;
      }

      const step = this.stepsById.get(cursor);
      if (!step) {
        terminal = "__fail__";
        runStatus = "failed";
        break;
      }

      const { result, nextOverride } = await this.runStepById(cursor);
      this.lastStatus = result.status ?? "ok";
      if (result.status === "failed" && result.error) this.lastError = result.error;
      this.attention.record(result.usage);

      if (result.status === "interrupted") {
        terminal = cursor;
        runStatus = "interrupted";
        this.interrupt = { stepId: cursor, awaiting: result.output.awaiting };
        break;
      }

      const nextCursor = nextOverride ?? this.selectNext(step, result);
      // A transition back into an already-executed step is one loop revolution.
      if ((this.visits.get(nextCursor) ?? 0) > 0) this.attention.revolution();
      cursor = nextCursor;
    }

    const outputs = this.projectOutputs();
    return {
      run_id: this.runId,
      status: runStatus,
      terminal,
      outputs,
      context: this.env.context,
      history: this.plugins.memory.readEventHistory(this.runId),
      budget: this.budgetOutcome,
      interrupt: this.interrupt,
      error: terminal === "__fail__" ? this.lastError : undefined,
    };
  }

  /** The first `escalate` step other than the current cursor, if any (§9.1). */
  private findEscalateStep(cursor: string): string | undefined {
    return this.doc.spec.steps.find((s) => s.type === "escalate" && s.id !== cursor)?.id;
  }

  /** §16.3 patch gate: is a run recorded on `recordedVersion` allowed to replay
   *  against this (possibly edited) document? Same version is always fine; a prior
   *  version must be explicitly listed in `spec.patch`. */
  private patchCompatible(recordedVersion?: string): boolean {
    if (!recordedVersion || recordedVersion === this.definitionVersion) return true;
    return (this.doc.spec.patch ?? []).includes(recordedVersion);
  }

  // ── one step: resolve → redact → (replay | cache | execute) → merge ────────
  private async runStepById(stepId: string): Promise<{ result: StepResult; nextOverride?: string }> {
    const step = this.stepsById.get(stepId);
    if (!step) {
      return { result: failResult("E_UNKNOWN_STEP", `no step ${stepId}`), nextOverride: "__fail__" };
    }

    // Trust point 2 — control policy (§13.1).
    if (this.plugins.governance.requirePolicy(step, this.grants) === "deny") {
      const result = failResult("E_POLICY_DENIED", `policy denied ${step.type}`);
      this.append(step, this.bumpVisit(step.id), {}, result);
      this.env.logical_tick++;
      return { result, nextOverride: "__fail__" };
    }

    // Trust point 3 — resolve + redact `in`.
    const resolved = resolveInputs(step.in, this.env.context);
    let input = this.plugins.governance.redact(resolved, this.grants);

    // Resume injection (§7.14): the paused step re-runs with the caller's payload
    // and a resume marker so wait/approval completes instead of pausing again.
    const resume = this.opts.resume;
    if (resume && resume.stepId === step.id) {
      input = { ...input, ...(resume.payload ?? {}), __resume: { timeout: !!resume.timeout } };
    }

    const { result, nextOverride } = await this.runOne(step, input);
    this.mergeIntoContext(step, result.output);
    return { result, nextOverride };
  }

  private async runOne(step: Step, input: Record<string, unknown>): Promise<{ result: StepResult; nextOverride?: string }> {
    const composite = COMPOSITE.has(step.type);
    let retryCount = 0;

    for (;;) {
      const attempt = this.bumpVisit(step.id);
      const existing = this.plugins.memory.lookupRecord(this.runId, step.id, attempt);
      let result: StepResult;
      const wasReplay = existing !== undefined;

      if (existing && !composite) {
        // §16.3 run-pinning: verify this step still computes what it recorded. The
        // idempotency key encodes (definitionVersion, step_id, input, space_id), so
        // a recomputed key that differs from the record means the rotor was edited
        // under the run — fail rather than silently diverge, unless a patch gate
        // declares the recorded version replay-compatible.
        const currentIdem = idempotencyKey(this.definitionVersion, step.id, input, this.spaceId);
        if (currentIdem !== existing.idempotency_key && !this.patchCompatible(existing.definitionVersion)) {
          const result = failResult(
            "E_REPLAY_DIVERGENCE",
            `step ${step.id}: recorded on ${existing.definitionVersion ?? "?"}, replaying on ${this.definitionVersion} (no patch gate)`,
          );
          return { result, nextOverride: "__fail__" };
        }
        // Pure replay — return the recorded result, no handler call (§5.4).
        result = recordToResult(existing);
      } else if (existing && composite) {
        // Re-run so children replay from their own records; keep recorded output.
        await this.runHandler(step, input);
        result = recordToResult(existing);
      } else {
        result = await this.physical(step, input, attempt);
      }

      this.env.logical_tick++;

      if (result.status !== "failed") return { result };

      // Failure routing (§5.5): retry (with backoff), then catch, else __fail__.
      const retrier = matchError(step.retry, result.error?.name);
      if (retrier && retryCount < (retrier.max_attempts ?? 1) - 1) {
        // Backoff only on FRESH failures — replay never sleeps (the retry
        // sequence is already recorded, and re-timing it would just be waste).
        if (!wasReplay) {
          const ms = (retrier.interval_ms ?? 0) * (retrier.backoff_rate ?? 1) ** retryCount;
          if (ms > 0) await this.sleep(ms);
        }
        retryCount++;
        continue;
      }
      const catcher = matchError(step.catch, result.error?.name);
      return { result, nextOverride: catcher ? catcher.next : "__fail__" };
    }
  }

  /** Execute a handler and append its StepRecord (fresh path), honoring the
   *  result cache (§5.7). */
  private async physical(step: Step, input: Record<string, unknown>, attempt: number): Promise<StepResult> {
    const cacheCfg = this.usableCache(step);
    const idem = idempotencyKey(this.definitionVersion, step.id, input, this.spaceId);
    const baseKey = cacheCfg && cacheCfg.key && cacheCfg.key !== "auto" ? cacheCfg.key : idem;
    // §5.7 scope isolation: confine reuse to the cache config's sharing boundary.
    const cacheKey = scopedCacheKey(baseKey, cacheCfg?.scope, {
      runId: this.runId,
      definitionVersion: this.definitionVersion,
      spaceId: this.spaceId,
    });

    let result: StepResult;
    let cacheFrames: Frame[] = [];

    if (cacheCfg) {
      const hit = this.plugins.memory.cacheGet(cacheKey, this.env.logical_tick);
      if (hit !== undefined) {
        result = { output: hit, frames: [], status: "ok" };
        cacheFrames = [cacheFrame("hit")];
        return this.append(step, attempt, input, mergeFrames(result, cacheFrames));
      }
    }

    result = await this.runHandler(step, input);

    // An interrupted step (§7.14 wait/approval pause) is NOT recorded: on resume
    // the run replays the completed prefix and re-runs this step with the injected
    // payload. Recording it would make replay re-pause forever.
    if (result.status === "interrupted") return result;

    if (cacheCfg && result.status === "ok") {
      this.plugins.memory.cachePut(cacheKey, result.output, {
        ttlTicks: parseTicks(cacheCfg.ttl),
        scope: cacheCfg.scope,
      });
      cacheFrames = [cacheFrame("miss")];
    }
    return this.append(step, attempt, input, mergeFrames(result, cacheFrames));
  }

  /** The step's cache config, or `undefined` if caching MUST be refused here.
   *  §5.7: a `tenant`/`global` entry whose key omits `space_id` is unsound (it
   *  could bind against a foreign HDC space), so an engine MUST refuse it — with
   *  no space bound we simply do not cache. */
  private usableCache(step: Step): CacheConfig | undefined {
    const cfg = this.resolveCache(step);
    if (!cfg) return undefined;
    if ((cfg.scope === "tenant" || cfg.scope === "global") && !this.spaceId) return undefined;
    return cfg;
  }

  /** Invoke the handler; a thrown error becomes a typed failed result. */
  private async runHandler(step: Step, input: Record<string, unknown>): Promise<StepResult> {
    const handler = this.handlers.get(step.type);
    if (!handler) return failResult("E_NO_HANDLER", `no handler for ${step.type}`);
    try {
      return await handler.execute({ step, input, env: this.env, plugins: this.plugins, engine: this.engine });
    } catch (e) {
      // Normalize to the taxonomy: a thrown RotorError keeps its specific code; a
      // raw throw is a handler defect → E_HANDLER (a non-taxonomy handler error).
      const err = RotorError.from(e, "E_HANDLER");
      return failResult(err.code, err.message);
    }
  }

  /** Build + append the StepRecord, return the result it records. */
  private append(step: Step, attempt: number, input: Record<string, unknown>, result: StepResult): StepResult {
    // §13.4 field-grain redaction: strip granted-out fields from the OUTPUT before
    // it is recorded, so they are absent from the event history, the Context (via
    // recordToResult below), and any log drain built from the record.
    const output = this.plugins.governance.redact(result.output, this.grants);
    const rec: StepRecord = {
      run_id: this.runId,
      step_id: step.id,
      attempt,
      logical_tick: this.env.logical_tick,
      input_hash: sha256(canonicalize(input)),
      idempotency_key: idempotencyKey(this.definitionVersion, step.id, input, this.spaceId),
      space_id: this.spaceId,
      definitionVersion: this.definitionVersion,
      principal: this.principal,
      agent_identity: { ref: this.agentRef, run_id: this.runId },
      status: result.status ?? "ok",
      output,
      frames: result.frames,
      usage: result.usage,
      error: result.error,
    };
    this.plugins.memory.appendStepRecord(rec);
    // Fan out to the log drain AFTER the durable write (§3.8). Fire-and-forget:
    // telemetry only, never affects the run (SPEC.md §17.6). Only fresh
    // executions reach here — replay short-circuits before append — so a replayed
    // run never re-emits.
    this.plugins.drain?.emit(rec);
    return recordToResult(rec);
  }

  // ── select_next — the control plane (§5.1) ─────────────────────────────────
  private selectNext(step: Step, result: StepResult): string {
    switch (step.type) {
      case "branch": {
        const cfg = step.config as { cases: Array<{ when: string; next: string }>; default?: string };
        for (const c of cfg.cases ?? []) {
          if (evalPredicate(c.when, this.env.context)) return c.next;
        }
        return cfg.default ?? step.next ?? "end";
      }
      case "gate": {
        const cfg = step.config as GateConfig;
        const verdict = String(result.output.verdict ?? "pass");
        if (verdict === "pass") return cfg.on_pass ?? step.next ?? "end";
        if (verdict === "escalate") return cfg.on_escalate ?? cfg.on_fail ?? step.next ?? "end";
        return cfg.on_fail ?? step.next ?? "end";
      }
      case "loop": {
        const cfg = step.config as LoopConfig;
        if (result.output.exhausted && cfg.on_exhausted === "refuse") return "end";
        return step.next ?? "end";
      }
      case "wait": {
        // On a resumed timeout, route via on_timeout (§7.14); otherwise continue.
        if (result.output.timedOut) {
          const cfg = step.config as { on_timeout?: "escalate" | "fail" | "resume" };
          if (cfg.on_timeout === "fail") return "__fail__";
          if (cfg.on_timeout === "escalate") return this.findEscalateStep(step.id) ?? step.next ?? "end";
        }
        return step.next ?? "end";
      }
      case "fail":
        return "__fail__";
      default:
        return step.next ?? "end";
    }
  }

  // ── Context merge (§5.2): step outputs are namespaced; out-names that match a
  //    declared state key are reduced into shared state. ──────────────────────
  private mergeIntoContext(step: Step, output: Record<string, unknown>): void {
    const ctx = this.env.context;
    ctx.steps[step.id] = { ...(ctx.steps[step.id] ?? {}), ...output };
    for (const key of Object.keys(output).sort()) {
      if (this.stateKeys.has(key) || key in this.reducers) {
        const reducer = (this.reducers[key] ?? "last-write-wins") as Parameters<typeof applyReducer>[2];
        ctx.state[key] = applyReducer(ctx.state[key], output[key], reducer);
      }
    }
  }

  private projectOutputs(): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (const p of this.doc.spec.outputs ?? []) out[p.name] = resolveRef(p.from, this.env.context);
    return out;
  }

  private resolveCache(step: Step): CacheConfig | undefined {
    if (step.cache === "none") return undefined;
    if (step.cache && typeof step.cache === "object") return step.cache;
    // A document-level default applies to cacheable leaf types (never `model`).
    if (this.doc.spec.cache && CACHEABLE_DEFAULT.has(step.type)) {
      return this.doc.spec.cache;
    }
    return undefined;
  }

  private bumpVisit(stepId: string): number {
    const n = this.visits.get(stepId) ?? 0;
    this.visits.set(stepId, n + 1);
    return n;
  }

  private async runRotor(ref: string, subInputs: Record<string, unknown>): Promise<RunResult> {
    const subDoc = this.opts.rotorResolver?.(ref);
    if (!subDoc) {
      // Unresolved reference is a hard failure, not a fake success (§7.19).
      return {
        run_id: `${this.runId}::${ref}`,
        status: "failed",
        terminal: "__fail__",
        outputs: {},
        context: { inputs: subInputs, state: {}, steps: {} },
        history: [],
      };
    }

    // §11.3 identity attenuation: a callee may only NARROW the caller's scopes,
    // never widen them. A scope the callee declares that the caller lacks is
    // refused; otherwise the callee runs under the INTERSECTION.
    const callerScopes = new Set(this.principal.scopes ?? []);
    const requested = subDoc.spec.identity?.scopes ?? [];
    const exceeded = requested.filter((s) => !callerScopes.has(s));
    if (exceeded.length > 0) {
      return {
        run_id: `${this.runId}::${ref}`,
        status: "refused",
        terminal: "__attenuation__",
        outputs: { refused: "E_SCOPE_EXCEEDED", scopes: exceeded },
        context: { inputs: subInputs, state: {}, steps: {} },
        history: [],
      };
    }
    const attenuated = requested.length > 0 ? requested.filter((s) => callerScopes.has(s)) : [...callerScopes];
    const calleePrincipal: Principal = { ...this.principal, scopes: attenuated };

    // Recursive execute; the callee shares the same plugins (stator, §17.1) and
    // runs under the attenuated identity — its StepRecords carry the narrowed scopes.
    return execute(subDoc, subInputs, this.plugins, {
      runId: `${this.runId}::${ref}`,
      principal: calleePrincipal,
      handlers: this.opts.handlers,
      rotorResolver: this.opts.rotorResolver,
      maxTicks: this.maxTicks,
    });
  }
}

// ───────────────────────────────────────────────────────────────────────────
// Helpers.
// ───────────────────────────────────────────────────────────────────────────

function validateInputs(doc: RotorDocument, inputs: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const p of doc.spec.inputs ?? []) {
    if (inputs[p.name] !== undefined) out[p.name] = inputs[p.name];
    else if (p.default !== undefined) out[p.name] = p.default;
    else if (p.required) throw new RotorError("E_MISSING_INPUT", `required input '${p.name}'`, { context: { input: p.name } });
  }
  // Carry through any extra inputs the caller supplied (permissive).
  for (const k of Object.keys(inputs)) if (!(k in out)) out[k] = inputs[k];
  return out;
}

function recordToResult(rec: StepRecord): StepResult {
  return {
    output: rec.output ?? {},
    frames: rec.frames ?? [],
    status: rec.status,
    usage: rec.usage,
    error: rec.error,
  };
}

function failResult(name: string, cause: string): StepResult {
  return { output: {}, frames: [{ type: "refuse", data: { error: name } }], status: "failed", error: { name, cause } };
}

function matchError<T extends Retrier | Catcher>(rules: T[] | undefined, name: string | undefined): T | undefined {
  if (!rules) return undefined;
  const n = name ?? "";
  return rules.find((r) => r.errors.includes(n) || r.errors.includes("*"));
}

function cacheFrame(disposition: "hit" | "miss" | "write"): Frame {
  return { type: "cache", disposition };
}

function mergeFrames(result: StepResult, extra: Frame[]): StepResult {
  if (extra.length === 0) return result;
  return { ...result, frames: [...result.frames, ...extra] };
}

function manifest(plugins: Plugins): RunContextEnvelope["capabilities"] {
  return {
    grounding: plugins.grounding.status(),
    memory: plugins.memory.status(),
    models: plugins.models.status(),
    connections: plugins.connections.status(),
    gateway: plugins.gateway.status(),
    governance: plugins.governance.status(),
    pool: plugins.pool.status(),
    drain: plugins.drain.status(),
  };
}

// Re-export cache types so downstream imports stay local to the executor module.
export type { CacheConfig, CacheScope };
