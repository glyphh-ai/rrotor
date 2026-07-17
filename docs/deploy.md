# Deploying rrotor

How a rotor runtime scales — from a single box to a multi-node Kubernetes fleet —
and why the design makes horizontal scale almost free.

This document covers `deploy/`: the `Dockerfile`, the `docker-compose.yml` local
stack, and the `deploy/k8s/` manifest set — the **fleet** topology (many
interchangeable pods behind one Service, sharing one stator). The runtime's HTTP
surface (the probe/run server) lives in `src/server.ts`.

> For the **product** topologies — the local `brew install glyphh` CLI and the
> fly.io **one-instance-per-session** model that the mobile/co-work/code apps use —
> see [docs/hosting.md](hosting.md). Same binary, same HTTP contract; different
> deployment shape.

---

## 1. The one idea: a rotor instance is stateless and fungible

Everything here follows from a single property of the runtime (SPEC.md §17.1,
docs/runtime.md §5.3):

> **No run-critical state lives in a rotor instance.** The Context (§5.2), the
> append-only event history of `StepRecord`s (§5.4), the HDC store, and the caches
> (§5.7) all live in the **external stator (memory) backend**. The gateway is the
> only transport boundary. Any conformant instance **MAY** serve any run — including
> one another instance started — by loading the run's pinned `definitionVersion` and
> continuing (or replaying) from the recorded event history.

This is the **fungibility invariant**, and it is exactly the Kubernetes per-instance
model: a rotor runtime is a **Deployment of interchangeable pods**, scaled
horizontally. A load balancer may route any request to any pod; a pod that dies is
replaced and the run continues on its successor, because the state was never in the
pod.

A loaded local model and warm caches *do* live in a pod — but that content is
**reconstructible optimization, never run-critical state** (SPEC.md §17.1). Managing
its reconstruction cost across the fleet is the job of pooling (§4 below).

---

## 2. The HTTP surface (`src/server.ts`)

A dependency-light `node:http` server — no framework, so the probe path has no
cold-start cost of its own. `startServer(port)` (default `8080`) exposes:

| Method + path | Purpose | k8s use |
| --- | --- | --- |
| `GET /healthz` | **Liveness** — the process is up and the event loop turns. No dependency checks. | `livenessProbe` |
| `GET /readyz` | **Readiness** — reports the runtime's capability manifest (docs/runtime.md §3.8); `200` when the pod can serve runs, `503` otherwise. | `readinessProbe` |
| `GET /version` | Build identity — the pinned runtime version. | rollout checks |
| `POST /run` | Accepts `{ rotor, inputs }`. Returns **`501 executor wiring pending`** for now — the Integrate stage connects it to the executor. | request path |

Why the split matters: **liveness** failing restarts *that pod only* and never
touches the run (state is external); **readiness** failing pulls the pod out of the
Service's endpoints — no traffic — without a restart, so a pod that has lost its
stator connection stops receiving requests until it recovers.

---

## 3. Two deployment tiers

### 3.1 Single-node (in-process store)

One pod, an **in-process** memory/event-history store (`Map`/JSON). Simplest to run;
the store lives in the pod, so it is **not** fungible — there is nothing to fail over
to. This is the open runtime's `pool-noop` design (docs/runtime.md §5.3): "the same
design with the pool plugin set to one cold pod — conformant, just not scaled."

Use it for local development, CI, and single-tenant boxes. `replicas: 1`, no shared
backend required.

### 3.2 Multi-node (shared Postgres stator)

Three or more pods behind one Service, all pointed at a **shared, external
Postgres + pgvector stator**. Now the invariant holds fleet-wide: run state is in the
stator, so any pod serves any run and the pool scales horizontally. This is the tier
`deploy/k8s/` describes.

The jump from tier 1 to tier 2 is **purely a configuration change** — swap the
in-process store for the shared Postgres backend (`ROTOR_STATOR_BACKEND=postgres`) —
because the runtime was stateless the whole time. No code path in `server.ts`
distinguishes "which pod I am."

> The shared-stator backend is the durable, cross-pod L3 requirement
> (docs/runtime.md §6.1 phase 5). The SQLite/Postgres stator backends are a later
> build phase; the in-process store ships first.

---

## 4. How pooling & affinity (§17) map to Kubernetes

SPEC.md §17 describes pooling and affinity as **determinism-neutral** runtime
optimizations (§17.6): they change latency and cost, never a run's recorded outputs
or transitions. Here is how each spec concept lands on k8s primitives.

| SPEC.md §17 concept | Kubernetes mechanism (`deploy/k8s/`) |
| --- | --- |
| Pool of fungible instances (§17.1) | The **Deployment** — a set of interchangeable pods. |
| `spec.pool.minHot` (§17.3) — warm floor | HPA `minReplicas: 3` + Deployment `replicas: 3`. |
| `spec.pool.maxHot` (§17.3) — hard cost ceiling | HPA `maxReplicas: 20`. |
| `spec.pool.targetConcurrency` (§17.3) — in-flight per instance | HPA custom `pending_rotors` Pods metric (`averageValue: 5`). |
| `coldStart.budget_ms` (§17.3) | HPA `scaleUp` behavior — react fast to a burst so callers don't each eat a cold-start. |
| Warm-pool bounded by budget (§17.3) | `maxReplicas` cap + conservative `scaleDown`; the platform may provision *down* from 20. Pre-warm is shed before live runs. |
| Instance affinity keys (§17.4) — `tenant`/`conversation`/`entity` | **Future affinity ring** (see §6). Today the Service is plain round-robin, which is always correct because affinity is `prefer` (soft) by design. |
| Determinism-neutrality (§17.6) | Which pod served a run is operational metadata only — never a control input. Round-robin routing can never change a result. |
| Cross-node availability | `podAntiAffinity` spreads pods across nodes; the **PDB** (`minAvailable: 2`) keeps the pool serving through drains. |

The autoscaling signal is expressed **portably** — requests-per-instance
(`pending_rotors`), independent of the mechanism that acts on it — exactly as
SPEC.md §17.3 specifies `targetConcurrency`.

---

## 5. The manifest set (`deploy/k8s/`)

Apply the whole set with `kubectl apply -k deploy/k8s`.

| File | Kind | Role |
| --- | --- | --- |
| `deployment.yaml` | Deployment | 3 fungible rotor pods; liveness `/healthz` + readiness `/readyz`; resource requests/limits; `podAntiAffinity` across nodes; `RollingUpdate` (`maxUnavailable: 0`); env from ConfigMap/Secret; non-root, read-only rootfs. |
| `service.yaml` | Service (ClusterIP) | One stable front; round-robin over ready pods (any request → any pod). |
| `hpa.yaml` | HorizontalPodAutoscaler | Scale 3→20 on CPU **and** the custom `pending_rotors` metric. |
| `configmap.yaml` | ConfigMap | `ROTOR_*` non-secret config: stator URL, model URL, channel, port. Identical on every pod. |
| `secret.yaml` | Secret (template) | Placeholder stator/model/channel credentials. **Do not commit real values** — source from a secret manager. |
| `pdb.yaml` | PodDisruptionBudget | Keep `minAvailable: 2` through voluntary disruptions. |
| `kustomization.yaml` | Kustomization | Applies the set as one unit. |

The **fungibility invariant is called out in comments** in each manifest — the
ConfigMap/Secret are identical on every pod and point at the *external* stator, the
Deployment's replicas/anti-affinity/rolling-update are all safe *because* state is
external, and the Service load-balances with no stickiness.

### Building the image

```sh
docker build -f deploy/Dockerfile -t rrotor:latest .
```

Multi-stage Node 20-slim: a `build` stage runs `npm ci` + `tsc`, then a lean
`runtime` stage copies only `dist` + production deps, runs as the non-root `node`
user, `EXPOSE`s `8080`, and carries a `HEALTHCHECK` that hits `/healthz` via `node`
(no `curl` in the image). `CMD` is `node dist/server.js`.

---

## 6. Local cross-pod demo (`docker-compose.yml`)

To make the multi-node story tangible on one machine:

```sh
docker compose -f deploy/docker-compose.yml up --build
curl localhost:8080/readyz   # rotor instance A
curl localhost:8081/readyz   # rotor instance B
```

Two `rotor` services (A on `:8080`, B on `:8081`) run against **one shared Postgres +
pgvector stator** and **one shared Redis** coordination channel. Because both pods
are fungible, a run started via A is continuable/replayable by B from the shared
event history — the same design as the k8s Deployment, shrunk to a laptop. This is
the local stand-in for interchangeable pods, and the demonstrable "cross-pod L3
story."

---

## 7. What's next (the future affinity ring)

Today routing is plain round-robin over the Service — always correct, because
instance affinity is a `prefer` (soft) preference (SPEC.md §17.4) that falls back to
any instance. The next step is an **affinity ring**: hash a request's affinity keys
(`tenant` / `conversation` / `entity`) and prefer the pod already warm for them, so
warm state and prompt-cache prefixes (§8.6) are reused rather than re-primed. The
`pending_rotors` queue and the Redis channel in the compose stack are the
coordination substrate that ring will read.

Crucially, that ring stays **determinism-neutral** (SPEC.md §17.6): it chooses
*where* a run executes, never *what* it computes. A warm instance carries no
authority of its own — every effect is still checked against the run's recorded grant
set. So the ring is a pure performance layer over the fungible pool this document
already describes.
