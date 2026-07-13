# Hosting OpenRotor

Where a rotor runtime runs, how to push one to a vendor, and the line between the
**open runtime** (you host it, anywhere, for yourself) and the **glyphh managed
service** (we host it, curated and bounded).

This document is about *placement* — which cloud, which primitives, what you bring.
For *how the runtime scales* once placed (pods, probes, the fungibility invariant),
see [deploy.md](deploy.md). For *what a runtime is*, see [runtime.md](runtime.md).

---

## 1. The deployable unit is a spec, not a service

The mental model is **Claude Code**, inverted onto infrastructure.

With Claude Code you hand an agent three things — a **git repo**, the **loop it
should run**, and the **models** it may call — and it executes against your code in
your environment. OpenRotor is the same shape, except the "environment" is itself a
**declared, deployable spec**:

| Claude Code gives the agent… | OpenRotor deploys… |
| --- | --- |
| a git repo | the **git repo** the rotor and its runtime config live in |
| a loop / task | the **rotor** — a versioned RotorSpec document (yours) |
| a set of models | the **model(s)** bound behind `ROTOR_LOCAL_MODEL_URL` |

That is the whole product surface: **pick a repo, pick a rotor, pick the models.**
The unit you deploy is not a bespoke server — it is the RotorSpec document plus the
runtime that interprets it. This is why the runtime is stateless and the spec is
typed: the spec *is* the deployable, and the type definition is what makes it
portable across the vendors below without rewriting anything.

> **Open-source stance: OpenRotor hosts nothing for anyone.** This repo is the
> runtime and the spec. What we ship is a runtime that is *trivial to push* to Fly
> or any other vendor — a Dockerfile, a Kustomize set, and the env contract in §6.
> The developer owns the account, the bill, and the box. The managed, curated
> version is glyphh's (§8), and it is a separate offering, not this repo.

---

## 2. The two planes decide the placement

Everything here follows from one split (see [deploy.md §1](deploy.md), SPEC.md §17.1):

| Plane | What it is | Profile | Where it wants to live |
| --- | --- | --- | --- |
| **Control plane** — rotor pods | the stateless Node runtime (`src/server.ts`): `load → validate → execute` | tiny, CPU-only (`250m`/`256Mi` request, `1`/`512Mi` limit) | anywhere cheap; scale-to-zero ideal |
| **Stator + channel** | Postgres+pgvector (all run-critical state) + Redis (pool coordination) | managed stateful services | a managed DB/cache next to the pods |
| **Model data plane** | local inference behind `ROTOR_LOCAL_MODEL_URL` (llama-server / ollama) | **GPU, bursty, idle-heavy** | a separate GPU service, scaled to zero |

The rotor pods are pennies. **Effectively all cost is the GPU model lane.** Because
the [fungibility invariant](deploy.md#1-the-one-idea-a-rotor-instance-is-stateless-and-fungible)
keeps *no* run-critical state in a pod, these three can sit on three different
providers without the runtime knowing. **Do not co-locate them by default** — host
the cheap stateless plane and the expensive GPU plane on the terms that suit each.

---

## 3. Primary target: Fly.io

Fly is the recommended first target for the rotor runtime, and the design already
assumes it: the pool plugin (`runtime.md §3.7`, `§5.3`) names **"Fly Machine
suspend/resume"** as its mechanism. Three properties line up exactly:

- **Machines are fast-booting Firecracker microVMs.** Start one per session,
  **suspend when idle, resume on demand** — a direct fit for `spec.pool`
  (`minHot` / `maxHot` / `coldStart.budget_ms`) and scale-to-zero economics.
- **microVM isolation per session** — the right boundary when the runtime is
  loading a *user's* git repo and a *user's* model (untrusted code + weights).
- **GPU Machines exist** (A10 / L40S / A100), so the model lane (§5) can run on the
  same platform when you want it there.

### 3.1 Push it

The runtime builds straight from `deploy/Dockerfile` (multi-stage Node 20-slim,
non-root, `EXPOSE 8080`, `HEALTHCHECK` on `/healthz`). A minimal `fly.toml`:

```toml
app = "my-rotor"
primary_region = "iad"

[build]
  dockerfile = "deploy/Dockerfile"

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = "suspend"   # suspend/resume — the pool's warmth story
  auto_start_machines = true
  min_machines_running = 0         # scale to zero when idle; raise for a warm floor

  [[http_service.checks]]
    method = "GET"
    path = "/readyz"               # capability manifest — 200 only when it can serve runs
    interval = "5s"
    timeout = "3s"

[[vm]]
  cpu_kind = "shared"
  cpus = 1
  memory = "512mb"                 # matches the k8s pod limit in deploy/k8s

[env]
  ROTOR_PORT = "8080"
  ROTOR_STATOR_BACKEND = "postgres"
```

```sh
fly launch --no-deploy          # create the app from fly.toml
fly ext postgres create         # managed Postgres (add pgvector) — the stator
fly redis create                # Upstash Redis — the coordination channel
fly secrets set \
  ROTOR_STATOR_URL=... ROTOR_STATOR_USER=... ROTOR_STATOR_PASSWORD=... \
  ROTOR_CHANNEL=... ROTOR_LOCAL_MODEL_URL=...
fly deploy
```

`min_machines_running = 0` gives you scale-to-zero; set it to your `spec.pool.minHot`
to keep a warm floor. The `/readyz` check is the readiness gate from
[deploy.md §2](deploy.md#2-the-http-surface-srcserverts) — Fly pulls an
unready Machine out of rotation exactly as a k8s readiness probe would.

---

## 4. Other vendors — the porting matrix

The runtime is a plain Docker image with an env contract (§6), so it ports cleanly.
Nothing below requires a code change — only wiring the stator, the channel, and (if
used) the model URL.

| Vendor | Fit | Runtime | Stator (pgvector) | Channel | GPU model lane | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| **Fly.io** | ★ primary | Machines, suspend/resume | Fly Postgres / Neon | Upstash Redis | Fly GPU Machines | design already targets it (§3) |
| **GKE / EKS** | ★ scale | apply `deploy/k8s/` as-is | Cloud SQL / RDS | Memorystore / ElastiCache | GPU node pool + Karpenter | the manifests' native home; HPA `pending_rotors`, PDB, anti-affinity land exactly as written |
| **Render** | good | Docker web service, autoscale | Render Postgres (+pgvector) | Render Key Value | — (external GPU) | simplest managed CPU tier |
| **Railway** | good | Docker service | Railway Postgres | Railway Redis | — (external GPU) | fastest to a running demo |
| **Plain Docker / VM** | any | `deploy/docker-compose.yml` | pgvector container | Redis container | local GPU / external | the compose stack *is* this, on one box |
| **Heroku** | ⚠ CPU only | web dyno (12-factor fit) | Heroku Postgres + pgvector | Key-Value Store | **none — no GPU** | fine for a control-plane prototype; **cannot host the model lane** — don't build on it |

Rule of thumb: **Fly to start** (per-session microVMs + suspend/resume + GPU on one
platform), **GKE/EKS when scale or enterprise procurement demands it** (the repo
already ships the manifests). Render/Railway are good managed CPU tiers if you keep
the GPU lane external. Heroku is a CPU-only prototype at best.

---

## 5. The GPU / model lane is a separate service

The rotor pod is **not** the GPU box. A `model` step calls the models plugin, which
hits whatever `ROTOR_LOCAL_MODEL_URL` points at — an OpenAI/Anthropic-compatible
endpoint (`llama-server`, `ollama`, or a hosted equivalent). That endpoint is a
**distinct, GPU-backed service** you point the runtime at. Because it is decoupled
by a URL, you host it wherever GPU is cheapest for your usage shape.

For the "user spins up a model, then it goes idle" pattern, GPU cost ranks:

1. **Serverless / scale-to-zero GPU** (Modal, RunPod, Baseten, Fly GPU) — best
   value here; you don't pay for idle accelerators, and per-second billing mirrors
   the gateway's own *local = free, frontier = metered* model. The cold-start you
   manage is exactly what the spec already models as `coldStart.budget_ms` + warm
   pools.
2. **Reserved / committed cloud GPU** — cheapest per hour *only* if kept busy ~24/7.
3. **Hyperscaler on-demand GPU** — most expensive per hour; justified only at
   steady high utilization.

Point `ROTOR_LOCAL_MODEL_URL` at the serverless GPU endpoint and the runtime is none
the wiser — the gateway keeps local inference free and off the metering path
([runtime.md §3.5](runtime.md), "local inference MUST NOT proxy the gateway").

---

## 6. What you bring — the env contract

The runtime is configured entirely through `ROTOR_*` environment variables (the
same set the `deploy/k8s/` ConfigMap/Secret and `docker-compose.yml` use). Porting
to any vendor is wiring these:

| Variable | Role | Example |
| --- | --- | --- |
| `ROTOR_PORT` / `PORT` | HTTP port the probe/run server binds | `8080` |
| `ROTOR_STATOR_BACKEND` | `inprocess` (single-node) or `postgres` (fungible fleet) | `postgres` |
| `ROTOR_STATOR_URL` | Postgres+pgvector connection — the stator | `postgres://host:5432/openrotor` |
| `ROTOR_STATOR_USER` / `ROTOR_STATOR_PASSWORD` | stator credentials (from a secret manager) | — |
| `ROTOR_CHANNEL` | Redis coordination channel (pool / `pending_rotors`) | `redis://host:6379/0` |
| `ROTOR_LOCAL_MODEL_URL` | the local model endpoint (§5) | `http://models:8000/v1` |

Two tiers, one flag ([deploy.md §3](deploy.md)): `inprocess` is a single fungible-free
box (SQLite, dev/CI); `postgres` makes the fleet fungible and horizontally scalable.
The jump between them is **config only** — the runtime was stateless the whole time.

---

## 7. Decision, in one line

- **Self-hosting the open runtime for yourself?** → **Fly** (start), graduate to
  **GKE/EKS** at scale. Point the model URL at a **serverless-GPU** endpoint.
- **Just want it running to try?** → **Railway / Render**, or `docker compose up`.
- **Want someone to host it, curated and bounded?** → that's **glyphh** (§8).

---

## 8. glyphh managed: the same three choices, bounded

When we productionize this runtime as **glyphh**, the hosting is ours and the model
set is **curated, not unbound.** The user still makes the same three choices from §1 —

- **their git repo,**
- **their rotor,**
- **their model(s)** —

but the third is chosen from a **curated, supported list**, not an arbitrary URL. We
restrict and govern which models run, on which lanes, at which cost. This is the
premium side of the plugin seams the open runtime leaves open:

| Concern | Open runtime (this repo) | glyphh managed |
| --- | --- | --- |
| **Who hosts** | you do, on your Fly/k8s/vendor account | glyphh does |
| **Model set** | any `ROTOR_LOCAL_MODEL_URL` you point at — unbounded | **curated, supported list; frontier lanes metered** |
| **Hosting** | you push the Docker image / manifests | K8s pool, Fly Machine suspend/resume, affinity ring (`runtime.md §3.7`) |
| **Governance** | local unenforced stubs, deny-by-default | org roles/grants, credits ledger, Ed25519 license |
| **Stator** | SQLite or your own Postgres | Postgres+pgvector, cascade consolidation |

Nothing about the **document** changes across that line — the same `.rotor` runs on
both, and capability negotiation ([runtime.md §3.8](runtime.md)) reconciles the
difference. Self-hosted, you get the full unbounded runtime and own the box.
Managed, glyphh curates the models and runs the pool — you keep the three choices,
we keep them safe and bounded.
