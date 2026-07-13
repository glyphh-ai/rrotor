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
| 0 | Test & CI harness | ⬜ | tooling |
| 1 | Structured logging + wire the real runtime | ⬜ | L1 hardening |
| 2 | Durable & shared stator (SQLite) | ⬜ | L1→L2 |
| 3 | **Log drains subsystem** | ⬜ | L2 (observability) |
| 4 | Attention budgets + gateway metering | ⬜ | L2 |
| 5 | Trust layer (access, identity attenuation, anomaly/firewall, merge) | ⬜ | L2 |
| 6 | HDC grounding law | ⬜ | L2 (headline feature) |
| 7 | wait/interrupt resume + approval | ⬜ | L2 |
| 8 | Run-pinning / patch gates / replay divergence | ⬜ | L3 |
| 9 | Gateway translation + prompt cache + model frontier/micro-rotor | ⬜ | L3 |
| 10 | Secondary handler completeness | ⬜ | L3 |
| 11 | Pooling (hot/warm/cold) + affinity | ⬜ | L3 |

Legend: ⬜ not started · 🟡 in progress · ✅ done

---

## Phase 0 — Test & CI harness  ⬜

**Why first:** there are currently **zero tests** and no lint. A critical,
methodical build needs the harness before the code, so every later phase lands
with proof. This phase builds the scaffolding that makes progress trackable.

### Tasks
- [ ] Add **Vitest** (`vitest`, `@vitest/coverage-v8`) as devDeps; `type: module`
      already set. Config `vitest.config.ts` with node environment.
- [ ] Add scripts: `test`, `test:watch`, `test:cov`, `lint` (eslint +
      `@typescript-eslint`), and a composite **`verify`** = `typecheck && lint &&
      test`. `verify` is the single command every phase gates on.
- [ ] Establish test layout: `test/unit/**`, `test/integration/**`,
      `test/conformance/**`, `test/fixtures/**` (sample `.rotor.yaml` docs +
      expected outputs).
- [ ] Build the **golden replay harness** (`test/harness/replay.ts`): run a rotor
      to completion, capture its `StepRecord[]` history, then re-run in replay
      mode and assert byte-identical outputs + identical `logical_tick` sequence.
      This is the core determinism guardrail reused by every later phase.
- [ ] Build a **capability-status assertion helper** (`test/harness/caps.ts`):
      assert a runtime's `manifest()` reports expected `{ready, tier}` per seam.
- [ ] Seed smoke tests against the existing base rotors in `rotors/` so the
      current happy path is pinned before we change anything.
- [ ] Wire CI: GitHub Actions workflow running `npm ci && npm run verify` on push
      to the branch. Add a coverage floor (start at 60%, ratchet up per phase).
- [ ] Add a `SessionStart` hook so web sessions can run tests/lint (use the
      `session-start-hook` skill).

### Acceptance
- [ ] `npm run verify` passes locally and in CI on a clean checkout.
- [ ] Golden replay harness proves determinism for `rotors/base.rotor.yaml`,
      `web-dev.rotor.yaml`, `corp-data-slim.rotor.yaml`.
- [ ] Coverage report is produced and the floor is enforced.

---

## Phase 1 — Structured logging + wire the real runtime  ⬜

**Why:** the biggest correctness-of-story gap (gap #1) — `Runtime` advertises all
seven seams as `planned()` stubs and never constructs `buildBasicPlugins()`, so
`/readyz`, the REPL, and the manifest all lie. This phase also lays the
**structured logging foundation** that Phase 3's drains ride on.

### Tasks
- [ ] Add `src/obs/logger.ts`: a small structured logger (`level`, `msg`, `fields`,
      `run_id`/`step_id` when in a run scope). JSON output when
      `ROTOR_LOG_FORMAT=json` (env already declared in `configmap.yaml:38`,
      currently unread), pretty otherwise. No external deps.
- [ ] Replace scattered `console.*` in `server.ts`, `cli.ts`, `repl.ts`,
      `banner.ts` with the logger (keep human-facing CLI stdout as-is; route
      diagnostics through the logger).
- [ ] Wire `Runtime` (`src/runtime/runtime.ts`) to construct the real
      `buildBasicPlugins()` and register **actual** capability statuses, not
      `planned()` stubs.
- [ ] Fix `/readyz` (`server.ts`) to reflect true per-seam readiness instead of
      `Object.keys(...).length > 0`; pod `ready` = all required seams ready.
- [ ] Make the REPL `validate`/`run` commands actually execute (remove the
      "lands next build phase" placeholders in `repl.ts:63`).
- [ ] Add load-time reconciliation: on rotor load, check `spec.requires` against
      the manifest and refuse/warn on unmet requirements (docs/runtime §3.8).
- [ ] Correct the stale docstring in `server.ts` ("501 until executor wired").

### Acceptance
- [ ] Integration test: boot server, `GET /readyz` reports the true tier map and
      flips to not-ready when a required seam is absent.
- [ ] REPL test: `validate` + `run` on a fixture return real results.
- [ ] Logger test: `ROTOR_LOG_FORMAT=json` emits one JSON object per line with the
      expected keys; run-scoped logs carry `run_id`.
- [ ] Manifest test: `manifest()` matches `buildBasicPlugins()` reality.

---

## Phase 2 — Durable & shared stator (SQLite)  ⬜

**Why:** gap #2 — event history + cache live in per-process, per-request Maps, so
the load-bearing §17.1 "state in the stator, fungible pods" claim is false and
cross-request replay is impossible. A durable stator is a prerequisite for
honest replay tests and for a stable log-drain append seam (Phase 3).

### Tasks
- [ ] Define a clean persistence boundary behind the existing `EventHistory` +
      cache interfaces in `src/exec/store.ts` (no interface change for callers).
- [ ] Implement a **SQLite** stator (`better-sqlite3` or `node:sqlite`) backing
      the event history (append-only), fact chains (`is_current` supersession),
      and the result cache with §5.7 scope isolation (`run|rotor|tenant|global`,
      tenant/global keyed with `space_id`).
- [ ] Thread a **single stator instance** through the server so it is constructed
      once per process, not per `/run` request (`server.ts:138`).
- [ ] Backend selected by `ROTOR_STATOR_BACKEND` / `ROTOR_STATOR_URL` (already in
      `configmap.yaml`); `memory` backend kept for tests/bare box.
- [ ] Enforce §5.7 cache scope on read (currently `scope` is stored but never
      gates reuse).

### Acceptance
- [ ] Replay test: run a rotor in one request, replay it in a **second** request
      against the same process — outputs + ticks identical (impossible today).
- [ ] Cache-scope test: a `tenant`-scoped entry is not reused across `space_id`;
      a `global` entry is.
- [ ] Restart test: history survives a store reopen (durable), returns identical
      replay.
- [ ] Golden replay harness passes on the SQLite backend and the memory backend.

---

## Phase 3 — Log drains subsystem  ⬜  ⭐ (explicit requirement)

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
- [ ] Define `DrainPlugin` interface in `plugins/interfaces.ts` (`emit(record)`,
      `flush()`, `status()`), add `drain` to `CAPABILITY_NAMES` and the `Plugins`
      bundle.
- [ ] Implement `plugins/drain.ts`:
  - [ ] `NoopDrain` (basic-tier default; `ready:true`, `tier:"basic"`).
  - [ ] `HttpDrain`: batched, newline-delimited JSON POST to `ROTOR_DRAIN_URL`
        with `ROTOR_DRAIN_TOKEN` bearer auth; configurable batch size / flush
        interval (`ROTOR_DRAIN_BATCH`, `ROTOR_DRAIN_FLUSH_MS`).
  - [ ] `FileDrain`: append NDJSON to a path (local dev / sidecar tailing).
- [ ] **Serializer** producing a stable drain envelope from a `StepRecord`
      (CloudEvents-ish, per the §14 aspiration): `run_id`, `step_id`, `attempt`,
      `logical_tick`, `status`, `principal`, `agent_identity`, `usage`, `frames`,
      `error`. Apply **field redaction** here (reuse governance redaction from
      Phase 5 when available; ship a config-driven allowlist now).
- [ ] **Reliability:** bounded in-memory buffer with backpressure (drop-oldest +
      counter, never block the run loop), retry with backoff on the sink, and a
      **flush-on-shutdown** hook tied to the server's SIGTERM path within the
      `terminationGracePeriodSeconds: 30` window (`deployment.yaml:54`).
- [ ] Decorate `appendStepRecord` to fan out to the drain after the durable write
      succeeds (drain failures never fail the run).
- [ ] Thread the drain through `buildBasicPlugins()` and the per-request bundle so
      HTTP runs are drained.
- [ ] Config wiring: add `ROTOR_DRAIN_*` to `deploy/k8s/configmap.yaml`;
      `ROTOR_DRAIN_TOKEN` to `deploy/k8s/secret.yaml` (never the ConfigMap).
- [ ] Docs: new `docs/observability.md` covering the drain envelope, config,
      redaction, and the telemetry-only invariant.

### Acceptance
- [ ] Unit: a run emits exactly N drain envelopes for N step records, with the
      expected redacted shape; determinism/replay unchanged with drain enabled
      (golden replay still green).
- [ ] Integration: `HttpDrain` against a local mock sink receives batched NDJSON;
      on sink 5xx it retries and does **not** fail the run.
- [ ] Backpressure: with the sink blocked, the run still completes and the buffer
      caps at its bound (drop counter increments, logged).
- [ ] Shutdown: SIGTERM flushes buffered envelopes before exit.
- [ ] `/readyz` shows `drain` seam status; misconfigured URL degrades, not throws.

---

## Phase 4 — Attention budgets + gateway metering  ⬜

**Why:** gap #3 — `spec.attention` (§10) and gateway `rateLimit`/`metering.budget`
(§8.4/§8.5) are typed but never read; the only bound is a hardcoded
`maxTicks = 10_000`. Governance `checkBudget` always returns `ok` and is never
called.

### Tasks
- [ ] Executor reads `spec.attention`: bound revolutions, tokens, cost, and wall
      time; on exhaustion apply `on_exhausted` = `stop | escalate | best-effort`.
- [ ] Raise the `budget-exceeded` escalation trigger (§9.1) automatically.
- [ ] Gateway (`gateway.ts`) enforces `rateLimit` (throttle/backoff) and
      `metering.budget` caps; usage counter shared via the stator, not per-request.
- [ ] Wire governance `checkBudget` into the loop (org/tenant caps, §13.2).
- [ ] Honor retry `interval_ms` / `backoff_rate` in `runOne` (currently ignored).

### Acceptance
- [ ] A rotor with a tiny `revolutions`/`tokens` budget stops/escalates exactly at
      the bound, deterministically.
- [ ] Rate-limit test: bursts are throttled; budget cap refuses with the typed
      error and an escalation frame.
- [ ] Retry test: observed backoff matches `interval_ms`/`backoff_rate`.

---

## Phase 5 — Trust layer  ⬜

**Why:** gaps #5, #7, #8 + redaction. The governable core of L2 is stubbed:
`firewall`/`anomaly` gates always `pass`, `spec.access` is never parsed,
`redactFields` is always undefined, sub-rotor identity attenuation is a comment
not code, and `E_UNMERGEABLE` is never raised.

### Tasks
- [ ] `E_UNMERGEABLE` (§5.2): undeclared concurrent writes error instead of
      silent last-write-wins (`executor.ts:399`, `flow.ts` parallel/map fan-in).
- [ ] Fix `parallel` map-mode leaking the loop var into shared `state[as]`
      (`flow.ts:84`).
- [ ] Sub-rotor **identity attenuation** (§11.3): callee runs under an intersected
      scope set; refuse on scope-exceed (`executor.ts:442`). Resolve/verify
      sub-rotor refs (no fake `ok`).
- [ ] Parse `spec.access` (§13.4): connector/field/stator grants; implement
      `governance.redactFields` and wire `redact` (used by Phase 3 drains too).
- [ ] Implement `firewall` (input) and `anomaly` (output) gates (§12.2/§14.2) —
      real scanning, anomaly emitted as a frame + escalation trigger, not a log
      line.
- [ ] Routing governance (§13.3): PII→local, approved-providers only.

### Acceptance
- [ ] Concurrent-write conformance test raises `E_UNMERGEABLE` unless a reducer is
      declared.
- [ ] Attenuation test: a sub-rotor requesting a scope the caller lacks is refused;
      the StepRecord shows the intersected identity.
- [ ] Redaction test: `spec.access`-marked fields are absent from outputs **and**
      from drain envelopes.
- [ ] Firewall/anomaly test: a malicious input is gated; an anomalous output emits
      an anomaly frame and triggers escalation.

---

## Phase 6 — HDC grounding law  ⬜

**Why:** gap #4 — the spec's headline feature (G3, §6.3, §7.3/§7.6) exists only as
a degraded exact-substring matcher; `encode` returns an empty cortex and margins
are hardcoded. This is the largest single body of work.

### Tasks
- [ ] Real hypervector encoder in `grounding.ts` (`hdc.map` §7.3): roles/segments/
      layers/cortex per the universal 7×33 schema; bind/bundle/permute algebra.
- [ ] Associative memory probe/verify with a real **margin**; the **hard logit
      gate** on `model` decode (§6.3) constraining decode to grounded fillers.
- [ ] `retrieve.kb` (§7.6) `probe|node|neighbors` over a real EntityGraph.
- [ ] `retrieve.vector` (§7.7) recorded-embedding lane with the embedding
      checkpointed at the boundary (currently lexical overlap, not checkpointed).
- [ ] `hdc-ground` gate and `assert` operate on real HDC margins.

### Acceptance
- [ ] Encode/decode round-trip test over the 7×33 schema with stable vectors
      (seeded, deterministic).
- [ ] Hard-gate test: decode cannot emit an ungrounded filler; margin below
      threshold refuses.
- [ ] `retrieve.kb` neighbors test returns graph-correct results; embeddings are
      checkpointed and replay-identical.

---

## Phase 7 — wait / interrupt resume + approval  ⬜

**Why:** gap #6 — `wait`/`interrupt` (§7.14) always terminates the run; there is no
resume channel, timer, or `on_timeout`, which also disables human-approval gates
end-to-end.

### Tasks
- [ ] Persist an interrupted run's state to the stator with a resume token.
- [ ] Add a resume path (CLI `resume` + `POST /runs/:id/resume`) that injects the
      recorded payload and continues from the recorded event history.
- [ ] Timer / `on_timeout` handling for `wait`.
- [ ] `approval` gate interrupts and resumes on decision.

### Acceptance
- [ ] Interrupt→resume integration test: a run pauses at `wait`, resumes with a
      payload, and completes; replay of the whole thing is deterministic.
- [ ] `on_timeout` fires and routes as declared.

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
