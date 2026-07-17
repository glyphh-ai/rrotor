# Hosting the runtime: one binary, three deployments

The rrotor runtime is **one artifact** — the `glyphh` CLI (this package's
`cli.js`). The same binary runs on a user's laptop and inside a cloud instance; the
only differences are configuration (env vars) and which surface talks to it. This
doc explains how that one binary is deployed three ways, how the glyphh control
plane assigns a cloud instance to a message thread, and where models run.

Prerequisite reading: the runtime is **stateless and fungible** (docs/deploy.md §1,
SPEC.md §17.1) — no run-critical state lives in an instance; it all lives in the
external stator (docs/vector-stores.md). That property is what makes "spin up an
instance per session" cheap and safe.

---

## 1. The uniform runtime contract

Every deployment exposes the identical contract, so a client never cares where the
runtime runs:

**HTTP surface** (`src/server.ts`, `glyphh serve`):

| Method · path | Purpose |
| --- | --- |
| `GET /healthz` | liveness — process up |
| `GET /readyz` | readiness — capability manifest; 200 = can serve |
| `GET /version` | build identity |
| `POST /run` | `{ rotor, inputs }` → executes; returns `{ run_id, trace_id, status, outputs, error? }` |
| `POST /runs/:id/resume` | continue an interrupted run (§7.14) |

**Configuration** (env — the only thing that changes between deployments):

| Env | Meaning | Local | Cloud |
| --- | --- | --- | --- |
| `ROTOR_STATOR_BACKEND` | `memory` \| `sqlite` \| `pgvector` | `sqlite` (a file) | `pgvector` (shared) |
| `ROTOR_STATOR_URL` | stator connection | `~/.glyphh/rotor.db` | Postgres URL |
| `ROTOR_LOCAL_URL` | local model lane endpoint | bundled / Ollama | Modal / fly GPU |
| `ROTOR_FRONTIER_URL` | frontier (metered) lane | a provider | a provider |
| `ROTOR_DRAIN_URL` | OTLP/telemetry sink | — | the collector |
| `PORT` | serve port | `8080` | `8080` |

Because the contract is uniform, **the client is deployment-agnostic**: it holds a
base URL (`http://127.0.0.1:8080` locally, or the instance's URL in the cloud) plus
an auth token, and calls the same endpoints.

---

## 2. Local — `brew install glyphh`

On the user's machine the runtime is a **local CLI**. `brew install glyphh` drops the
`glyphh` binary; it runs the runtime on `localhost` against a durable **SQLite**
stator file under `~/.glyphh`. Nothing leaves the machine unless a rotor calls an
external tool or the frontier model lane.

```
glyphh serve                 # start the local runtime daemon (localhost:8080)
glyphh run base.rotor.yaml   # one-shot run through the executor
glyphh repl                  # interactive
glyphh errors / support …    # the supportability tools (docs/support.md)
```

The desktop/mobile client, when it detects a local runtime, points its base URL at
`localhost` — same API as the cloud. Lifelong memory lives in the local SQLite file,
so it is the user's own data on their own disk (docs/memory.md "where the stator
lives").

Distribution: `deploy/homebrew/glyphh.rb` is the formula skeleton (a bottle built
from the packaged CLI). `pkg`/`bun build`/a Node SEA can produce a self-contained
binary so the formula has no Node dependency; until then the formula depends on
`node`.

---

## 3. Cloud, per session — fly.io

On the mobile/web app there is no localhost, so the glyphh infrastructure **spins up
a runtime instance per code session** and binds it to the glyphh message thread.

### The topology

```
 client (mobile/web)                 glyphh control plane            fly.io
 ─────────────────────               ────────────────────           ───────────────
  pick rotor + perm mode   ──POST──▶  thread → Machine map   ──API──▶  Machine (glyphh serve)
  add files                           (create/start/stop)              │  the runtime
  chat / cowork / code                                                 │  stator: Postgres+pgvector
        ▲                                                              │  model lane: Modal / fly GPU
        └────────────── run results / stream ◀──────────────────────── ┘
```

- A **code session** ↔ a **glyphh message thread** ↔ a **fly Machine** running
  `glyphh serve`. The control plane (glyphh-server) owns the `thread_id → machine_id`
  mapping and drives the fly Machines API: **create/start** on first message, **stop**
  when idle (fly Machines stop-to-zero, so an idle session costs nothing), **destroy**
  when the session ends.
- Fly assigns the Machine; the control plane records its URL against the thread. The
  client always addresses "the runtime for this thread" — it never sees fly.
- The runtime image is the existing `deploy/Dockerfile` (already stateless,
  non-root, `/healthz`); `deploy/fly.toml` is the Machine config.

### State & fungibility in the cloud

The Machine's disk is **ephemeral** — fine, because the runtime holds no run-critical
state. The stator is external **Postgres + pgvector** (docs/vector-stores.md), so:

- A session that resumes on a fresh Machine continues from the recorded event history
  (§5.4) and the durable facts — the E5/E6/E7 work makes this real and live.
- Lifelong memory (docs/memory.md tiers) persists in Postgres across Machine restarts,
  scoped per user/tenant.

### The remote surface is deliberately tiny

Everything the user controls remotely is three things, all `POST /run` parameters:

1. **which rotor** — the spec (path through the stator) they select in the client UI;
2. **permission mode** — the grant set / governance policy for the run (§3.6);
3. **files** — uploaded through the client, staged into the Machine (or object
   storage the rotor reads).

That is the entire remote control surface. Chat / co-work / code are **client
presentations** over this one API — the runtime is surface-agnostic; the rotor
definition and permission mode shape behaviour (see §5).

---

## 4. Models — where inference runs

The models plugin (`src/plugins/models.ts`) already splits **local (free)** vs
**frontier (metered)** lanes behind one interface, selected per step. Hosting just
sets the two endpoints:

- **Local lane** — small open-weight models. Options: a **Modal** endpoint (serverless
  GPU, scale-to-zero, good for bursty per-session load) or **fly GPU** Machines
  (co-located with the runtime, lower latency, simpler network). Recommendation:
  start with a **handful of small open-weight models on Modal** behind `ROTOR_LOCAL_URL`
  — scale-to-zero matches the per-session burst pattern, and it keeps GPUs off the
  runtime's critical path. Revisit fly GPU if latency or egress cost bites.
- **Frontier lane** — a metered provider via `ROTOR_FRONTIER_URL` (the gateway
  translates wire formats, §8.3; usage is metered, §8.5).

Determinism is unaffected: model calls are the quarantined data plane (§7.2) — tokens
are recorded, never reproduced; the control plane never depends on them.

---

## 5. The three surfaces — one runtime

Chat, co-work, and code look different to the user but are the **same runtime API**;
what differs is the rotor + permission mode + client rendering:

| Surface | Rotor shape | Permission mode | Client rendering |
| --- | --- | --- | --- |
| **Chat** | conversational (ask → answer, grounded) | read-only / no tools | message bubbles |
| **Co-work** | task loop with tools/connections (MCP) | scoped tool grants | doc/artifact canvas |
| **Code** | base `ask→plan→execute→test` + code tools | repo + exec grants | editor / diff / terminal |

The runtime doesn't branch on "surface" — it runs whichever rotor the client selects
under whichever grant set. Adding a surface is a client + rotor-definition change, not
a runtime change.

---

## 6. Security at the boundary

- **AuthN/Z** — the client presents a token; the control plane authorizes the
  thread→Machine binding and injects the run principal (§11). The Machine is reachable
  only through the control plane, not publicly.
- **Permission mode** — resolved to a grant set at run start (§3.6), enforced at the
  three governance points (step gate, field redaction, budget). "Read-only chat" vs
  "code with exec" is a grant-set difference, not a code path.
- **Secrets / connections** — Pipedream Connect creds live on Pipedream per user
  (docs/glyphh-integration.md); the runtime holds only the short-lived connect token
  the control plane mints, never long-lived secrets.
- **Isolation** — one Machine per session is a hard tenancy boundary; the stator is
  scoped per user/tenant.

---

## 7. Open decisions (recommendations, not yet locked)

1. **Local transport: HTTP daemon vs stdio.** The doc assumes a localhost HTTP daemon
   (uniform with cloud). A stdio mode (`glyphh serve --stdio`) would let the desktop
   app speak to the CLI without a port. *Lean:* ship HTTP now (uniform), add stdio if
   a port collides or for tighter desktop embedding.
2. **Cloud stator: shared vs per-session Postgres.** One shared Postgres+pgvector
   (tenant-scoped rows) is simplest and matches the live-read model (E7). A
   per-session database is stronger isolation but heavier. *Lean:* shared, tenant-
   scoped; revisit if a tenant needs physical isolation.
3. **Models host: Modal vs fly GPU.** *Lean:* Modal for the local lane (scale-to-zero,
   bursty), revisit fly GPU for latency.
4. **Self-contained binary.** Node SEA / `bun build` to drop the `node` dependency
   from the brew formula. Scoped follow-up.

---

## 8. What ships in this repo

- `deploy/Dockerfile` — the runtime image (already stateless/non-root; `glyphh serve`).
- `deploy/fly.toml` — the fly.io Machine config for a per-session instance.
- `deploy/homebrew/glyphh.rb` — the Homebrew formula skeleton for `brew install glyphh`.
- `deploy/k8s/*` — the fleet topology (docs/deploy.md), for a shared multi-pod runtime.
- `package.json` `bin` exposes both `rrotor` and `glyphh`.

The control plane (thread→Machine mapping, fly Machines API calls, token minting)
lives in **glyphh-server**, not here — this repo is the runtime it drives.
