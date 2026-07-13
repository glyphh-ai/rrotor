# OpenRotor Build Plan

**Status tracker for the OpenRotor runtime build.** This is the single source of
truth for what we are building, in what order, and how we know each piece is
done. Update the checkboxes as work lands. Every phase is test-first: a phase is
not "done" until its acceptance tests are green in CI.

- Spec: [`SPEC.md`](SPEC.md) (RotorSpec 0.1, normative)
- Runtime design: [`docs/runtime.md`](docs/runtime.md)
- Execution model: [`docs/execution-model.md`](docs/execution-model.md)
- Branch: `claude/landing-location-1vazfq`

---

## How to use this document

1. Work phases **top to bottom**. Later phases assume earlier ones landed.
2. Each phase has **Tasks** (the work) and **Acceptance** (the tests that must
   pass). Check a task with `[x]` when its code + tests are merged and green.
3. Do not check a phase's **Gate** box until *every* task and acceptance box in
   it is checked and `npm run verify` is green on the branch.
4. Keep the **Progress dashboard** below in sync — it is the at-a-glance view.

### Conformance north star (from SPEC §1.4)

| Level | Bar | Target phase |
| --- | --- | --- |
| **L1 Basic** | valid parse, deterministic loop, checkpoint/replay in-process | mostly present today; hardened by Phase 2 |
| **L2 Standard** | + durable/shared stator, trust layer (identity, access, anomaly), budgets enforced | Phases 2–7 |
| **L3 Full** | + portable replay, run-pinning/patch, pooling, gateway translation | Phases 8–11 |

---

## Progress dashboard

| # | Phase | Gate | Conformance |
| --- | --- | --- | --- |
| 0 | Test & CI harness | ✅ | tooling |
| 1 | Structured logging + wire the real runtime | ✅ | L1 hardening |
| 2 | Durable & shared stator (SQLite) | ✅ | L1→L2 |
| 3 | **Log drains subsystem** | ✅ | L2 (observability) |
| 4 | Attention budgets + gateway metering | ✅ | L2 |
| 5 | Trust layer (access, identity attenuation, anomaly/firewall, merge) | ✅ | L2 |
| 6 | HDC grounding law | ✅ | L2 (headline feature) |
| 7 | wait/interrupt resume + approval | ✅ | L2 |
| 8 | Run-pinning / patch gates / replay divergence | ⬜ | L3 |
| 9 | Gateway translation + prompt cache + model frontier/micro-rotor | ⬜ | L3 |
| 10 | Secondary handler completeness | ⬜ | L3 |
| 11 | Pooling (hot/warm/cold) + affinity | ⬜ | L3 |

Legend: ⬜ not started · 🟡 in progress · ✅ done

---

## Phase 0 — Test & CI harness  ✅

**Why first:** there are currently **zero tests** and no lint. A critical,
methodical build needs the harness before the code, so every later phase lands
with proof. This phase builds the scaffolding that makes progress trackable.

### Tasks
- [x] Add **Vitest** (`vitest`, `@vitest/coverage-v8`) as devDeps; `type: module`
      already set. Config `vitest.config.ts` with node environment.
- [x] Add scripts: `test`, `test:watch`, `test:cov`, `lint` (eslint +
      `typescript-eslint`), and a composite **`verify`** = `typecheck && lint &&
      test`. `verify` is the single command every phase gates on.
- [x] Establish test layout: `test/unit/**`, `test/integration/**` (added as
      phases need it), `test/conformance/**`, `test/harness/**` (shared helpers +
      fixtures over the shipped `rotors/`).
- [x] Build the **golden replay harness** (`test/harness/replay.ts`): run a rotor
      to completion, capture its `StepRecord[]` history, then re-run in replay
      mode and assert byte-identical outputs + identical `logical_tick` sequence,
      and that replay appends **zero** new records. Core determinism guardrail.
- [x] Build a **capability-status assertion helper** (`test/harness/caps.ts`):
      assert a runtime's manifest reports expected `{ready, tier}` per seam.
- [x] Seed smoke tests against the existing base rotors in `rotors/` so the
      current happy path is pinned before we change anything.
- [x] Wire CI: GitHub Actions workflow (`.github/workflows/ci.yml`) running
      `npm ci && npm run verify` + coverage on push/PR. Coverage floor enforced
      in `vitest.config.ts` (Phase 0: ~50% lines/branches; ratchets up).
- [x] Add a `SessionStart` hook (`.claude/hooks/session-start.sh` +
      `.claude/settings.json`) so web sessions install deps and can run
      tests/lint.

### Acceptance
- [x] `npm run verify` passes locally (typecheck + lint + 18 tests green).
- [x] Golden replay harness proves determinism **and** zero-append replay for
      `rotors/base.rotor.yaml`, `web-dev.rotor.yaml`, `corp-data-slim.rotor.yaml`.
- [x] Coverage report is produced and the floor is enforced (`npm run test:cov`).

**Landed:** `package.json` (scripts + devDeps), `vitest.config.ts`,
`eslint.config.js`, `tsconfig.test.json`, `test/harness/{replay,caps,fixtures}.ts`,
`test/conformance/base-rotors.test.ts`, `test/unit/{parser,caps}.test.ts`,
`.github/workflows/ci.yml`, `.claude/`. Removed dead `W` const in `banner.ts`.

---

## Phase 1 — Structured logging + wire the real runtime  ✅

**Why:** the biggest correctness-of-story gap (gap #1) — `Runtime` advertised all
seven seams as `planned()` stubs and never constructed `buildBasicPlugins()`, so
`/readyz`, the REPL, and the manifest all lied. This phase also laid the
**structured logging foundation** that Phase 3's drains ride on.

### Tasks
- [x] Add `src/obs/logger.ts`: a small structured logger (`level`, `msg`, `fields`,
      `run_id`/`step_id` via `child()` scope). JSON output when
      `ROTOR_LOG_FORMAT=json` (env in `configmap.yaml:38`), pretty otherwise.
      Injectable sink + clock. No external deps.
- [x] Route runtime diagnostics (`server.ts` boot line, run complete/error) through
      the logger; human-facing CLI/REPL stdout kept as-is.
- [x] Wire `Runtime` (`src/runtime/runtime.ts`) to construct the real
      `buildBasicPlugins()` and register **actual** capability statuses, not
      `planned()` stubs. `Runtime.plugins` exposed for reuse.
- [x] Fix `/readyz` (`server.ts`) to reflect true per-seam readiness (pod `ready`
      = every advertised seam ready), extracted as pure `computeReadiness()`.
- [x] Make the REPL `validate`/`run` commands actually execute — factored into a
      TTY-free `execCommand()`; removed the "lands next build phase" placeholders.
- [x] Add load-time reconciliation (`Runtime.reconcile`): derive required seams
      from a rotor's step types and warn on unmet against the manifest (§3.8).
      (RotorSpec has no top-level `spec.requires`; needs are implied by step type.)
- [x] Correct the stale docstrings in `server.ts` ("501 until executor wired").

### Acceptance
- [x] Integration test (`test/integration/server.test.ts`): boots the server on an
      ephemeral port; `/healthz`, `/readyz` (true tier map), `/version`, and
      `POST /run` all drive over the wire. `computeReadiness` unit test proves the
      flip to not-ready when a seam is down.
- [x] REPL test (`test/unit/repl.test.ts`): `validate` + `run` return real results.
- [x] Logger test (`test/unit/logger.test.ts`): JSON one-object-per-line with
      level/msg/ts; `child()` bindings carry `run_id`; level filtering; env config.
- [x] Manifest test (`test/unit/runtime.test.ts`): manifest == `plugins.status()`,
      all seams ready + basic (no "planned").

**Landed:** `src/obs/logger.ts`, rewrote `src/runtime/runtime.ts` (real plugins +
`reconcile`), `src/server.ts` (`computeReadiness`, logger, `/run` reconciliation),
`src/repl.ts` (`execCommand`). Tests: logger, runtime, repl, server integration —
**43 tests green**, coverage floor raised to ~58%.

---

## Phase 2 — Durable & shared stator (SQLite)  ✅

**Why:** gap #2 — event history + cache lived in per-process, per-request Maps, so
the load-bearing §17.1 "state in the stator, fungible pods" claim was false and
cross-request replay was impossible. A durable stator is a prerequisite for
honest replay tests and for a stable log-drain append seam (Phase 3).

### Tasks
- [x] Factor the closed-op fact engine into `src/exec/facts.ts` (pure functions)
      so both backends share byte-identical semantics; refactor `InProcessStore`
      to use it (behavior-identical — conformance suite unchanged).
- [x] Implement a **SQLite** stator (`better-sqlite3`; stable API, prebuilt
      binaries, Node-20 compatible) backing the event history (append-only), fact
      chains (`is_current` supersession), result cache, kv, and turns — behind the
      same `Stator` interface (added optional `close()`).
- [x] Thread a **single stator instance** through the server (`statorFromEnv`);
      a fresh plugin bundle per `/run` keeps per-run plugin state isolated while
      history/cache/facts persist across requests via the shared store.
- [x] Backend selected by `ROTOR_STATOR_BACKEND` / `ROTOR_STATOR_URL`
      (`src/exec/stator.ts`); `memory` backend is the default (tests/bare box).
- [x] Enforce §5.7 cache scope via a scope-aware effective key (`scopedCacheKey`):
      `run`/`rotor`/`tenant`/`global`. Per SPEC §5.7, `tenant`/`global` keys carry
      `space_id`, and caching is **refused** when scope is tenant/global with no
      space bound (`usableCache`).

### Acceptance
- [x] Cross-request replay (`server.test.ts`): two `POST /run`s of the same rotor
      return the same run id and identical outputs/history — the second replays
      the first from the shared stator (impossible before).
- [x] Cache-scope test (`cache-scope.test.ts`): `run` isolates per run; `rotor`
      shares across runs; `tenant`/`global` both isolate by `space_id`.
      *(Correction: the spec — §5.7 — requires `global` to carry `space_id` too;
      an earlier draft of this acceptance wrongly said a global entry is reused
      across spaces. Both tenant and global isolate by space.)*
- [x] Restart durability (`sqlite-store.test.ts`): a run recorded to a file
      replays byte-identically from a freshly-reopened store.
- [x] Golden replay + cross-backend parity (`backends.test.ts`): determinism and
      zero-append replay pass on **both** backends, and a run's shape is identical
      across memory and SQLite. Plus a full Stator-surface parity test.

**Landed:** `src/exec/facts.ts`, `src/exec/sqlite-store.ts`, `src/exec/stator.ts`,
`scopedCacheKey`/`usableCache` (util + executor), shared stator in `server.ts`,
`better-sqlite3` dep. Tests: backends parity, sqlite store, cache scope, +
cross-request replay. **66 tests green**, coverage floor raised to ~64%.

---

## Phase 3 — Log drains subsystem  ✅  ⭐ (explicit requirement)

**Why:** the runtime has a rich, fully-typed, identity-stamped `StepRecord`/`Frame`
event stream (`types.ts:708-780`) but **no way to forward it anywhere**. This phase
adds structured **log drains**: an outbound sink that streams run events to an
external destination for audit, FinOps, and observability — honoring SPEC's
"**telemetry only, never a control predicate**" invariant (`SPEC.md:2230, 2372`).

### Design
- New **eighth capability seam** `drain`, following the existing plugin convention
  (`registry.ts` `CAPABILITY_NAMES`, `interfaces.ts`, `plugins/index.ts`,
  `buildBasicPlugins()`, `manifest()`), so it auto-appears in `/readyz` and
  degrades (`ready:false`) rather than throwing.
- **Tap point:** the single choke point every event flows through —
  `MemoryPlugin.appendStepRecord` (`memory.ts:91` → `store.ts:118`). The drain is a
  strictly fire-and-forget **observer**; it must never affect control flow or the
  purity invariant (`executor.ts:17`).

### Tasks
- [x] Define `DrainPlugin` interface in `plugins/interfaces.ts` (`emit(record)`,
      `flush()`, `close()`, `status()`), add `drain` to `CAPABILITY_NAMES` and the
      `Plugins` bundle.
- [x] Implement `plugins/drain.ts`:
  - [x] `NoopDrain` (basic-tier default; `ready:true`, `tier:"basic"`).
  - [x] `HttpDrain`: batched, newline-delimited JSON POST to `ROTOR_DRAIN_URL`
        with `ROTOR_DRAIN_TOKEN` bearer auth; configurable `ROTOR_DRAIN_BATCH`.
  - [x] `FileDrain`: append NDJSON to a path (local dev / sidecar tailing).
- [x] **Serializer** (`toEnvelope`) producing a stable `com.openrotor.step.v0`
      envelope from a `StepRecord` (run/step/attempt/tick, status, space, principal,
      agent, usage, frames, output, error). **Field redaction** applied here via
      `ROTOR_DRAIN_REDACT` (governance redaction routes through here in Phase 5).
- [x] **Reliability** (`BufferedDrain`): bounded buffer with backpressure
      (drop-oldest + counter, never blocks the run), retry-with-backoff (drop after
      N, never throws), auto-ship on full batch, and `close()` flush wired to the
      server's SIGTERM/SIGINT path within the `terminationGracePeriodSeconds` window.
- [x] Executor `append()` fans out to `plugins.drain.emit(rec)` after the durable
      write — fire-and-forget; **only fresh executions emit**, so replay never
      re-emits.
- [x] Thread one shared drain through `buildBasicPlugins({ drain })` and the
      per-request server bundle (parallel to the shared stator).
- [x] Config wiring: `ROTOR_DRAIN_*` + `ROTOR_LOG_LEVEL` in
      `deploy/k8s/configmap.yaml`; `ROTOR_DRAIN_TOKEN` in `deploy/k8s/secret.yaml`.
- [x] Docs: `docs/observability.md` — envelope, config, redaction, reliability,
      invariants.

### Acceptance
- [x] Unit (`drain.test.ts`): envelope shape + redaction; batching order;
      backpressure drop-oldest + counter; retry delivers; drop-after-retries never
      throws.
- [x] Integration (`drain.test.ts`): a run emits exactly N envelopes for N records;
      **run shape identical with and without a drain** (determinism preserved);
      **replay never re-emits**; `HttpDrain` delivers batched NDJSON to a mock sink
      and **retries a transient 500** without failing the run.
- [x] Shutdown: `shutdown()` flushes buffered envelopes to the sink before closing.
- [x] `/readyz` advertises the `drain` seam (server integration test).

**Landed:** `src/plugins/drain.ts` (`toEnvelope`, `NoopDrain`, `BufferedDrain`,
`HttpDrain`, `FileDrain`, `drainFromEnv`), `DrainPlugin`/`DrainEnvelope` +
8th seam, executor `emit` fan-out, shared drain + `shutdown()` in `server.ts`,
`docs/observability.md`, configmap/secret wiring. **80 tests green**, coverage
floor ~66%.

---

## Phase 4 — Attention budgets + gateway metering  ✅

**Why:** gap #3 — `spec.attention` (§10) and gateway `rateLimit`/`metering.budget`
(§8.4/§8.5) were typed but never read; the only bound was a hardcoded
`maxTicks = 10_000`. Governance `checkBudget` always returned `ok` and was never
called.

> **Determinism boundary (the load-bearing decision):** a budget is a *control
> input* — `on_exhausted` changes transitions — so it MUST be a pure function of
> recorded state, never wall-clock (§6, §17.6), or replay would diverge. Hence the
> deterministic dimensions (**revolutions / tokens / cost**, all recomputable from
> the event history) are enforced as control predicates; **wall-clock** dimensions
> (`AttentionBudget.wall_ms`, `rateLimit` rpm/tpm) are **not** control predicates
> and are handled — if at all — as transport timing outside the control plane.

### Tasks
- [x] Executor reads `spec.attention`: bounds **revolutions** (loop re-entries),
      **tokens**, and **cost**; on exhaustion applies `on_exhausted` =
      `stop | escalate | best-effort`. Surfaced as `RunResult.budget` and a
      `__budget__` terminal. (`src/exec/budget.ts` `AttentionMeter`.)
- [x] Raises the `budget-exceeded` escalation trigger (§9.1) automatically — routes
      to the rotor's `escalate` step when `on_exhausted: escalate`.
- [x] Wired `governance.checkBudget` into the loop (org/tenant cap seam, §13.2;
      basic tier never blocks, premium can).
- [x] Honors retry `interval_ms` / `backoff_rate` in `runOne` via an injectable
      `sleep` — **fresh failures only; replay never sleeps**.
- [~] **Deferred (transport, not control):** gateway `rateLimit` throttle
      (rpm/tpm/concurrency) and `wall_ms` — wall-clock quantities cannot be
      determinism-safe control predicates. They belong in an outcome-neutral
      transport layer (throttle delays a call, never changes a recorded outcome)
      and are tracked as a follow-up, not enforced as transitions here. The
      deterministic **cost/token cap** already covers the "budget cap refuses"
      need; `metering.budget` as a distinct gateway cap folds into it.

### Acceptance
- [x] A rotor with a tiny `revolutions` budget stops/escalates exactly at the bound
      (`test/integration/budget.test.ts`), **deterministically and replay-identical**.
- [x] `on_exhausted: escalate` routes to the escalate step (escalation frame via
      that step's normal recorded path).
- [x] Retry test: observed backoff sequence matches `interval_ms * backoff_rate^n`
      on fresh execution, and is **empty on replay**.
- [x] Unit: `AttentionMeter` exhaustion on revolutions/tokens/cost with precedence
      (`test/unit/budget.test.ts`).

**Landed:** `src/exec/budget.ts` (`AttentionMeter`), executor budget enforcement +
`RunResult.budget` + `findEscalateStep`, retry backoff with injectable `sleep`,
`governance.checkBudget` hook, `test/fixtures/looping.rotor.yaml`. **91 tests
green**, coverage floor raised to ~70%.

---

## Phase 5 — Trust layer  ✅

**Why:** gaps #5, #7, #8 + redaction. The governable core of L2 was stubbed:
`firewall`/`anomaly` gates always `pass`, `spec.access` never parsed,
`redactFields` always undefined, sub-rotor identity attenuation a comment not
code, and `E_UNMERGEABLE` never raised.

### Tasks
- [x] `E_UNMERGEABLE` (§5.2): a concurrent (parallel fan-in) write to a key with
      no declared reducer now raises the typed error instead of silent
      last-write-wins (`flow.ts`). (Sequential writes are ordered, not concurrent,
      so they keep last-write-wins correctly.)
- [x] Fixed `parallel` map-mode leaking the loop var into shared `state[as]` —
      save/restore around the fan-out (`flow.ts`).
- [x] Sub-rotor **identity attenuation** (§11.3): the callee runs under the
      INTERSECTION of caller grants and its own declared scopes; a scope the caller
      lacks is **refused** (`__attenuation__`, `E_SCOPE_EXCEEDED`); the sub-run's
      StepRecords carry the narrowed identity. Unresolved ref is a hard failure,
      not a fake `ok` (`executor.ts`).
- [x] `spec.access.redact` (§13.4) added to types + schema; `governance.resolveGrants`
      populates `redactFields`; the executor redacts step **outputs** at `append()`
      so redacted fields are absent from the record, the Context, AND drain envelopes.
- [x] `firewall` (input, blocks → fail) and `anomaly` (output, escalates + refuse
      frame) gates (§12.2/§14.2) via a deterministic pattern scanner (pii/policy).
- [~] **Deferred:** routing governance (§13.3, PII→local / approved-providers) —
      determinism-neutral model routing that needs model-step plumbing; tracked as
      a follow-up (composes with the Phase 9 gateway/model work).

### Acceptance
- [x] Concurrent-write test raises `E_UNMERGEABLE` unless a reducer is declared
      (`test/integration/trust.test.ts`).
- [x] Attenuation test: a callee requesting a scope the caller lacks is refused;
      the sub-run's StepRecord shows the intersected identity `["scope:a"]`.
- [x] Redaction test: `spec.access`-marked fields are absent from outputs **and**
      from drain envelopes.
- [x] Firewall/anomaly test (`test/unit/gate.test.ts`): PII/injection input is
      gated (fail); an anomalous output escalates and emits a refuse frame.

**Landed:** `flow.ts` (E_UNMERGEABLE + map save/restore + sub-rotor refused
passthrough), `executor.ts` (attenuation + output redaction), `governance.ts`
(`redactFields` from `spec.access.redact`), `gate.ts` (`scanForAnomaly`),
`types.ts` + `rotor.schema.json` (`access.redact`). Tests: `trust.test.ts`,
`gate.test.ts`. **102 tests green**, coverage floor raised to ~72%.

---

## Phase 6 — HDC grounding law  ✅

**Why:** gap #4 — the spec's headline feature (G3, §6.3, §7.3/§7.6) existed only as
a degraded exact-substring matcher; `encode` returned an empty cortex and margins
were hardcoded. The largest single body of work.

> **IP boundary (decided with the user):** `grounding.ts` previously stated the
> open runtime "does not practice the patent" — the real HDC engine was the closed
> premium tier. The user explicitly chose to implement the real hypervector engine
> + hard gate in the open runtime; that docstring was updated to describe the
> implemented engine. Flagged before writing any code.

> **Decision (embedding fidelity, basic tier):** the basic-tier embedding is a
> **deterministic local embedding** — hashed n-gram → fixed-dim vector, cosine
> similarity — chosen because it is **replay-safe by construction** (no RNG, no
> model call), a real upgrade over substring matching, and swappable for a neural
> lane later. A neural/frontier embedding is a **premium** lane that MUST be
> **checkpointed** into the `StepRecord` at the embedding boundary (§7.7) to stay
> replay-safe. **Short-term memory** (embedding every inbound prompt + outbound
> completion into the stator's turn buffer, recalled by cosine) is built here, not
> earlier — decided with the user during Phase 2.

### Tasks
- [x] Real hypervector engine `src/exec/hdc.ts` (`hdc.map` §7.3): deterministic
      seeded bipolar symbols (mulberry32), `bind`/`bundle`/`permute`/`cosine`, and
      `encodeRoleFillers` + `cleanup`. Replay-safe by construction (§6.2).
- [x] **Deterministic local embedding** `src/exec/embedding.ts`: pure
      `embed(text) → number[]` (hashed word + char-trigram, L2-normalized) +
      `cosine`. Replay-safe; the basis for short-term recall.
- [x] **Short-term memory**: `prompt` (inbound) and `model` (outbound) handlers
      `recordTurn` their text; `semanticRecall` ranks by **embedding cosine** over
      recorded turns (was lexical unit-dot).
- [x] `grounding.ts` rewritten to the HDC engine: an entity's cortex is the bundle
      of its `role⊗filler` binds; `probe` unbinds by role + cleans up for a real
      **margin**; `verify` grounds a claim iff it clears the margin threshold.
- [x] **Hard grounding gate** on `model` decode (§6.3): decode is constrained to
      `groundedFillers`; no grounded continuation → refuse (`E_UNGROUNDED`); a
      decoded output that fails `verify` (low margin) → refuse.
- [x] `retrieve.kb` (§7.6) `probe` (HDC recall) / `node` (entity edges) /
      `neighbors` (co-referent entities over the fact graph); `verify` uses HDC.
- [x] `retrieve.vector` (§7.7) rides the deterministic-local embedding via
      `semanticRecall`; the step output (hits) is checkpointed like any leaf step,
      so replay is identical.

### Acceptance
- [x] HDC round-trip (`test/unit/hdc.test.ts`): seeded symbols are deterministic
      and near-orthogonal; `bind` is its own inverse; encode → unbind → cleanup
      recovers the bound filler with a clear margin (> 0.2).
- [x] Hard-gate test (`test/integration/grounding.test.ts`): a `model` step decodes
      into a grounded filler when one exists, and **refuses** (`E_UNGROUNDED`) when
      none exists; `verify` refuses when the margin threshold is not met.
- [x] `retrieve.kb` neighbors returns graph-correct co-referents (ada→bob via a
      shared filler, not carol); `node` returns the entity's edges. Probes are
      deterministic (identical on repeat).

**Landed:** `src/exec/hdc.ts`, `src/exec/embedding.ts`, rewrote `grounding.ts`
(HDC), `memory.ts` (embedding recall + `recordTurn`), model hard gate, `retrieve.kb`
graph modes, turn recording in `prompt`/`model`. Tests: `hdc`, `embedding`,
`grounding`. **119 tests green**, coverage floor raised to ~74%.

---

## Phase 7 — wait / interrupt resume + approval  ✅

**Why:** gap #6 — `wait`/`interrupt` (§7.14) always terminated the run; there was no
resume channel, timer, or `on_timeout`, which also disabled human-approval gates
end-to-end.

> **Determinism key:** the interrupted step is **not recorded**. Resume replays the
> completed prefix from the event history and re-runs the paused step with the
> injected payload — recording the pause would make replay re-pause forever.

### Tasks
- [x] Interrupted-run state persists in the stator (`server.ts` `kvSet('run:'+id')`
      = doc + inputs + interrupt point); the run id is the resume token.
- [x] Resume path `POST /runs/:id/resume` (executor `ExecuteOptions.resume` injects
      the payload into the paused step and continues from the recorded history).
      `RunResult.interrupt` surfaces the pause point.
- [x] `on_timeout` handling for `wait` — a timeout resume routes via `select_next`
      (`fail` / `escalate` / continue).
- [x] `approval` gate interrupts without a decision and resolves on
      `approve`/`reject` when resumed.
- [~] **Deferred:** a CLI `resume` verb — it needs a durable (sqlite) backend to
      carry the paused run across processes and more `cli.ts` plumbing; the executor
      + HTTP paths fully cover the mechanism. Thin follow-up.

### Acceptance
- [x] Interrupt→resume (`test/integration/resume.test.ts`): pauses at `wait`
      (records only the completed prefix), resumes with a payload to completion, and
      the resumed history is **deterministic** across repeats.
- [x] `on_timeout` fires and routes as declared (timeout → `__fail__`).
- [x] `approval` gate routes on the decision (`approve`→yes, `reject`→no).
- [x] Server resume over HTTP (`test/integration/server-resume.test.ts`): `/run`
      pauses + persists, `/runs/:id/resume` completes; unknown run → 404.

**Landed:** executor resume (`ExecuteOptions.resume`, `RunResult.interrupt`,
no-record-on-interrupt, `wait` routing in `select_next`), `control.ts` wait handler,
`gate.ts` approval resolve, `server.ts` persist + `/runs/:id/resume`. Tests:
`resume`, `server-resume`. **126 tests green**, coverage floor raised to ~76%.

---

## Phase 8 — Run-pinning / patch gates / replay divergence  ⬜

**Why:** gap #9 (§16.3) — `definitionVersion` is recorded but there are no `patch`
markers and no failure on replay divergence, so portable/long-lived replay (L3)
is unsafe.

### Tasks
- [ ] `patch(marker)` support and version-pinned replay.
- [ ] Detect and fail on replay divergence (recorded vs recomputed) with a typed
      error.

### Acceptance
- [ ] Changing a rotor without a patch marker fails replay of an old run with the
      divergence error; with a marker it replays cleanly.

---

## Phase 9 — Gateway translation + prompt cache + model frontier/micro-rotor  ⬜

**Why:** gap #10 — the §8.3 MCP↔API↔provider translation table throws
`E_UNTRANSLATABLE` for everything but identity; prompt-cache lowering is a no-op;
the `model` frontier lane, micro-rotor (propose/dispose/backtrack), and
cache-aware `usage` breakdown are absent.

### Tasks
- [ ] Implement the protocol translation table + adapters.
- [ ] Prompt-cache breakpoint lowering (§8.6) and cache-aware usage accounting.
- [ ] Model micro-rotor with `max_backtracks`; reachable frontier lane behind
      config.

### Acceptance
- [ ] Translation round-trip tests across the protocol matrix.
- [ ] Cache test shows `cache_read`/`cache_write` usage populated correctly.
- [ ] Micro-rotor backtrack test bounded by `max_backtracks`.

---

## Phase 10 — Secondary handler completeness  ⬜

**Why:** gap #11 — remaining partial handlers: `retrieve.kb` graph/neighbors (done
in P6 if reached), `cascade` consolidation, `write` NL absorb enricher, `plan`
typed-enum decode, and most `tool.app`/substrate tools.

### Tasks
- [ ] `cascade` short→mid→long consolidation with summaries/absorb.
- [ ] `write` `mode: absorb` NL enricher (beyond single `raw.text` slot).
- [ ] `plan` typed-enum constrained decode.
- [ ] Flesh out substrate + `tool.app` methods (currently ~6 seeded, app methods
      no-op).

### Acceptance
- [ ] Per-handler unit tests; conformance fixtures for each step type pass.

---

## Phase 11 — Pooling (hot/warm/cold) + affinity  ⬜

**Why:** `pool.ts` is a conformant no-op (`route → local-0`, always `hot`);
§17.2/§17.3 hot/warm/cold and budget-bounded warming, plus `spec.affinity`, are
unimplemented. Telemetry-only invariant (§17.6) must hold.

### Tasks
- [ ] Implement instance-state tracking (telemetry only, never a control
      predicate).
- [ ] `spec.pool` / `spec.affinity` routing with budget-bounded warming.

### Acceptance
- [ ] Routing test respects affinity; warming stays within budget; instance state
      never gates control flow.

---

## Cross-cutting standards (apply to every phase)

- **Test-first:** write the failing acceptance test, then the code.
- **Determinism guardrail:** the golden replay harness (Phase 0) must stay green
  after every phase — it is the contract the whole runtime rests on.
- **Degrade, never raise:** new plugins report `status()` and degrade rather than
  throwing (`plugins/index.ts:2`).
- **Telemetry only, never control:** metrics/instance-state/drains observe; they
  never feed back into control flow (`SPEC.md:2372`).
- **Coverage ratchets up** each phase; CI blocks regressions.
- **One phase per PR** (or a small stack), each gated on `npm run verify`.
