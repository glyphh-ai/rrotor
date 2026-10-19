# Self-hosting rrotor

rrotor is the runtime your Ada models run in ([architecture](../architecture.md)). You run it yourself, on your own
machine, cluster or cloud account, at your own address; Glyphh does not host it. It is one image,
`ghcr.io/glyphh-ai/rrotor:<version>` (and `:latest`), with the Ada part built in; the licence, not the image, says
whether Ada runs.

A self-hosted rrotor is **one box**: one container, one `/data`, one public host. There is no high availability and
no autoscaling; a second copy of the same runtime is not a replica, it takes the link over from the first.

## The plans

| | Free | Pro, $99/month | Team, $299/month | Enterprise, by contract |
|---|---|---|---|---|
| Nodes in one cluster | 1 | 3 | 10 | unlimited |
| Precedents per node, across its models | 5,000 | 50,000 | 500,000 (TBD) | unlimited |
| Sync between nodes | no | yes | yes | yes, with high availability |
| Air-gapped | no | optional | optional | yes |
| Audit log | no | no | no | yes |

**Built:** the signed licence key that carries these limits, Community (Free's limits) without one, and the
precedent cap per node ([Licensing](#licensing)); structured logs, the audit log (Enterprise) and log drains, each
node its own ([Logs, audit and drains](#logs-audit-and-drains)); a cluster's membership: nodes that find and
authenticate each other, a head, the key's node cap at join and sync gated by the key ([A cluster](#a-cluster)).
**Not built yet:** what a cluster shares (replication of the stores, models, keys and licence from the head),
failover, high availability, the head gathering the cluster's drain and audit log, and the packages' cluster
settings; until then each node of a cluster serves from its own `/data`. The
decided design is [`specs/licensing.md`](../specs/licensing.md) and [`specs/cluster.md`](../specs/cluster.md); prices are the
server's (`glyphh-ai/server`, `docs/architecture.md`).

## The three ways

| | What you run | What it needs | Guide |
|---|---|---|---|
| **Docker Compose** | rrotor and Caddy (HTTPS) on one machine: `docker compose up -d` | A Linux machine with Docker and Compose v2, ports 80 and 443 reachable, a DNS name pointing at it | [compose.md](compose.md), `deploy/compose/` |
| **Helm** | A StatefulSet of one replica with its own volume, a Service and an Ingress | Kubernetes 1.33 or later that allows the sandbox's privileges (below), an Ingress controller, a storage class; cert-manager if you want it to issue the certificate | [helm.md](helm.md), `deploy/helm/rrotor/` |
| **Terraform** | One VM on AWS or Google Cloud running the Compose package, `/data` on its own disk | An AWS account or a Google Cloud project, and a DNS zone (Route 53 or Cloud DNS) or a name you point yourself | [terraform.md](terraform.md), `deploy/terraform/` |

Each starts the same way:

1. **Register the runtime** on the platform (`https://platform.glyphh.ai`, Getting started): a name and the public
   host it will answer at, e.g. `rrotor.acme.com`. The platform shows the runtime id and token **once**, and the
   snippet for the way you chose, filled in.
2. **Deploy** with that snippet.
3. **Add the connector** in Claude (Settings, Connectors) or any MCP client: `https://<public host>/mcp`. Signing in
   to Glyphh there is enough; a runtime key works too.

## The environment

What rrotor reads in `self` mode. Each package sets these for you.

| Variable | Required | Meaning |
|---|---|---|
| `RROTOR_MODE=self` | yes | A self-hosted rrotor |
| `RROTOR_RUNTIME_ID`, `RROTOR_RUNTIME_TOKEN` | yes, together | From registering the runtime on the platform. The token is this rrotor's identity to Glyphh: keep it secret |
| `RROTOR_PUBLIC_HOST` | yes | The exact host it answers at, e.g. `rrotor.acme.com`. Any other `Host` is refused (421 `E_HOST`) |
| `RROTOR_SERVER` | no, default `https://api.glyphh.ai` | The Glyphh server |
| `GLYPHH_LICENSE_KEY` | no | The licence key, for air-gapped use (normally it arrives over the link or the heartbeat; without one, Community's limits). `RROTOR_LICENCE` is its alias; also read from `/etc/rrotor/license.key` or `/data/licence.token`, at start, hourly and on `SIGHUP` (`PUT /v1/licence` is for a local rrotor only) |
| `RROTOR_LINK` | no, default `on` | `off` for air-gapped: no link and no heartbeat; rrotor runs on its licence key from the variable or the file |
| `RROTOR_KEYS_SHA256` | air-gapped | The SHA-256 of each runtime key that may call it, comma-separated (`printf %s "$KEY" \| sha256sum`). With the link on, the keys come down the link and this is not needed. With the link off there is no Glyphh sign-in either: runtime keys are the only way in |
| `RROTOR_DATA` | no, default `/data` | The persistent volume. One rrotor writes it, never two |

What it logs and where that goes are [below](#logs-audit-and-drains).

rrotor listens on port **7777**, plain HTTP: TLS is ended in front of it (Caddy, an Ingress, a load balancer), which
must pass the `Host` header through unchanged. `GET /healthz` is open and answers while the process is up;
`GET /v1/health` (authenticated) reports the link, each sandbox layer, the licence, each drain and whether
server-side logging is on.

## A cluster

Built so far: membership, with nothing shared yet (each node serves from its own `/data`, and every node opens the
runtime's link). Several self-hosted rrotors of one runtime, each with its own volume, behind your load balancer:
they find each other, authenticate each other with mutual TLS derived from one secret you make, and one of them,
the **head**, admits the others up to your key's `max_nodes`. A key without cluster sync (Community, Free) never
starts it: rrotor logs `cluster synchronisation disabled under the Community licence` and runs alone.

- **The port:** **7778** (`RROTOR_CLUSTER_PORT`), node to node only. Open it between the nodes and nowhere else;
  never put it behind the public load balancer.
- **The secret:** `openssl rand -base64 32`, the same on every node, kept as you keep the runtime token: anyone with
  it can join (up to `max_nodes`). To change it, set the new one in `RROTOR_CLUSTER_SECRET` and the old one in
  `RROTOR_CLUSTER_SECRET_PREVIOUS` on each node in turn, then remove the old one.
- **Readiness:** `GET /readyz` (open) is 200 while a node may take traffic and 503 with the reason (`joining`,
  `no_cluster`, `refused`) otherwise: point the load balancer's health check at it, and liveness at `/healthz`.
- **The node past `max_nodes`** is refused (`E_CLUSTER_FULL`), logged at error, kept out of the load balancer (it
  answers 503 to everything but `/healthz` and `/readyz`), keeps its `/data`, and asks again every minute.
- **Looking at it:** `GET /v1/cluster` (or the MCP tool `cluster_status`): the cluster's id, the head, every member,
  the nodes refused. The health has a short `cluster`.
- **Removing a node:** `rrotord cluster leave <node id>` on any node (`docker compose exec rrotor rrotord cluster
  leave rrotor-2`, or `kubectl exec`); it does not ask to join again until `rrotord cluster join` is run on it. These
  are not on the API: no runtime key can change the cluster.

| Variable | Default | Meaning |
|---|---|---|
| `RROTOR_CLUSTER` | `off` | `on`: join or make a cluster (a key with cluster sync; `RROTOR_MODE=self` only) |
| `RROTOR_NODE_ID` | the hostname | This node's name, `[a-z0-9-]{1,63}`: the pod name on Kubernetes |
| `RROTOR_CLUSTER_PEERS` | | `host:port,…` of every node, the same on each (Compose, Terraform). One of this and `RROTOR_CLUSTER_DNS` |
| `RROTOR_CLUSTER_DNS` | | A name whose A/AAAA records are the nodes: a headless Service with `publishNotReadyAddresses: true` (Kubernetes); resolved every 10 s |
| `RROTOR_CLUSTER_PORT` | `7778` | The cluster's listener; not the API's port |
| `RROTOR_CLUSTER_ADVERTISE` | `<node id>:<port>` | How the other nodes reach this one |
| `RROTOR_CLUSTER_SECRET` | | Required with the cluster on: 32 or more random bytes, base64 |
| `RROTOR_CLUSTER_SECRET_PREVIOUS` | | Trusted too, while a new secret rolls out |
| `RROTOR_CLUSTER_HEAD` | the lowest node id | The node that makes the cluster the first time. Without it, the lowest id makes it once every peer has answered: set it if a peer may be down at the very first start |
| `RROTOR_CLUSTER_EVICT_AFTER` | `24h` | A member unseen this long is removed by the head (`s`, `m`, `h`, `d`) |
| `RROTOR_CLUSTER_LOG_KEEP` | `1g` | The replication log kept for catch-up (`k`, `m`, `g`); read now, used once replication is built |

## Logs, audit and drains

**Everything stays on rrotor by default.** Nothing leaves it unless you set a drain (your own collector) or turn on
server-side logging (Glyphh). The heartbeat and the link's usage are licensing, not telemetry, and keep their fixed
fields.

- **Logs:** JSON lines on stdout, one per event, tokens masked; every line names the node (`node_id`, and in a
  cluster `cluster_id` and `role`), and the run
  (`run_id`, `model`, `version`, `function`) and the trace (`trace_id`, `span_id`) where there is one. Docker keeps
  them rotated (the Compose package: 10 MB a file, five files); on Kubernetes the kubelet rotates them. A run's own
  `ctx.log` lines stay in its log on `/data` (`GET /v1/runs/{id}/log`).
- **The audit log** (Enterprise): one event per call (REST, MCP or the link) and its end, deploy, change to the
  runtime keys, licence key taken, expired or refused, 401 or 403, server-side logging turned on or off, and a
  cluster made, joined, refused or left; who did
  it (a runtime key's SHA-256, never the key; a Glyphh sign-in's subject; the local token; Glyphh; rrotor itself),
  from which address, and how it ended. Kept in `/data/audit/<YYYY-MM-DD>.jsonl`, one file a day, each event fsynced;
  read with `GET /v1/audit` (`since`, `until`, `kind`, `actor`, `model`, `run_id`, `limit`, `cursor`; newest first)
  or the MCP tool `audit_query`. Below Enterprise the same events are log lines (`logger: rrotor.audit`), not kept,
  and `GET /v1/audit` is 403 `E_NOT_IN_PLAN`.
- **A drain** to your own OpenTelemetry collector: logs, metrics, traces and audit events by OTLP over HTTP with
  protobuf, never through Glyphh. Set `OTEL_EXPORTER_OTLP_ENDPOINT` (and OpenTelemetry's other variables as you need
  them); with none, there is no drain. Metrics: `rrotor.runs`, `rrotor.run.seconds`, `rrotor.precedents` and
  `rrotor.precedents.cap`, `rrotor.link.state`, `rrotor.drain.dropped`; traces: a span per API request (your
  `traceparent` honoured), with the sandbox's start and the function under it. While the collector is down, logs,
  metrics and traces wait in memory, `RROTOR_DRAIN_BUFFER` per signal, the oldest dropped and counted; audit events
  are never dropped: the drain reads them from `/data/audit` from its cursor, so a collector down for a day gets
  the day's events when it is back.
- **Server-side logging** (`RROTOR_SERVER_LOGS=on`): the same signals also go to Glyphh, so the platform can show
  them. rrotor says so plainly at every start and in its health. Refused with the link off.
- **Never sent anywhere, never logged:** a model's inputs and outputs, `ctx.log`'s fields, secrets, tokens and keys,
  request bodies. `RROTOR_LOG_PAYLOADS=on` adds inputs and results (4 KB each) to the run's spans and its `run.end`
  audit event, for your own drain only; Glyphh never gets them, nor your callers' addresses.

| Variable | Default | Meaning |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | | Your collector, e.g. `http://otel-collector:4318` (`/v1/logs`, `/v1/metrics`, `/v1/traces` are added); or per signal, `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`, `…_METRICS_ENDPOINT`, `…_TRACES_ENDPOINT`, as given. None: no drain |
| `OTEL_EXPORTER_OTLP_HEADERS`, `…_CERTIFICATE`, `…_CLIENT_CERTIFICATE`, `…_CLIENT_KEY`, `…_COMPRESSION`, `…_TIMEOUT` | | As OpenTelemetry defines them (and their per-signal forms) |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` | The only one offered: `grpc` stops rrotor at start (`E_CONFIG`); a collector takes HTTP and forwards gRPC |
| `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_SERVICE_NAME` | , `rrotor` | Added to every signal; rrotor adds `service.version`, `service.instance.id` (the node id), `rrotor.runtime.id`, `rrotor.node.role` and `rrotor.mode` |
| `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` | `always_on`, `always_off`, `traceidratio` or their `parentbased_` forms, and the ratio |
| `RROTOR_DRAIN_SIGNALS` | `logs,metrics,traces,audit` | What the drain sends |
| `RROTOR_DRAIN_BUFFER` | `10000` | Records held per signal while the collector is down |
| `RROTOR_DRAIN_AT` | `head` | `head` or `node`. Each node exports itself for now; once the head gathers the cluster's signals (not built yet), `head` exports for every node |
| `RROTOR_LOG_PAYLOADS` | `off` | `on`: inputs and results in your drain |
| `RROTOR_SERVER_LOGS` | `off` | `on`: also to Glyphh (`{RROTOR_SERVER}/v1/otlp/v1/…`, with the runtime token). Not with `RROTOR_LINK=off` |
| `RROTOR_AUDIT_RETENTION_DAYS` | `90` | Days of audit log kept (Enterprise) |
| `RROTOR_AUDIT_MAX_MB` | `1024` | The most the audit log may take; past it the oldest day goes (never today's). Each deletion is logged |
| `RROTOR_LOG_LEVEL` | `INFO` | The log level |

## The container's privileges, and why

Every model run is sandboxed inside rrotor: bubblewrap gives it its own user, mount, PID and network namespaces,
Landlock limits the files it can touch, a seccomp filter refuses the dangerous system calls, and an egress proxy is
its only way out ([architecture, section 4](../architecture.md#4-rrotor-inside)). Making namespaces and mounting a
fresh `/proc` from inside a container is exactly what a container runtime's defaults forbid, so rrotor's container
needs three of them lifted:

| Setting | Docker | Kubernetes | Why |
|---|---|---|---|
| seccomp unconfined | `--security-opt seccomp=unconfined` | `seccompProfile: Unconfined` | The default profile refuses `unshare` and `clone` with new namespaces |
| AppArmor unconfined | `--security-opt apparmor=unconfined` | `appArmorProfile: Unconfined` | The default profile refuses mounts, which bwrap needs for the run's filesystem |
| `/proc` unmasked | `--security-opt systempaths=unconfined` | `procMount: Unmasked` (with `hostUsers: false`) | A runtime masks parts of `/proc`; the kernel then refuses to mount a new `/proc` for the run's PID namespace |

On Ubuntu 23.10 and later the host also needs `kernel.apparmor_restrict_unprivileged_userns=0` (or an AppArmor
profile that lets the container make user namespaces); without it bwrap cannot bring up the run's loopback. The
Terraform modules set it; the Compose guide says how.

What is **not** asked for: no `--privileged`, no added capabilities, no host network, no host paths. rrotor runs as
an unprivileged user (uid 10001) on a read-only root filesystem, and its own seccomp filter, set with
`no_new_privs`, holds every run more tightly than the runtime's default profile would. If a layer cannot work,
`GET /v1/health` names it and rrotor refuses runs (`E_SANDBOX_UNAVAILABLE`) rather than run them unsandboxed.

## What it reaches, outbound

| To | Why | When |
|---|---|---|
| `api.glyphh.ai:443` | The link (a WebSocket, outbound only), the daily licence heartbeat, and checking a Glyphh sign-in | `RROTOR_LINK=on` |
| `ghcr.io:443` | Pulling the image | Installing and upgrading |
| Let's Encrypt (`acme-v02.api.letsencrypt.org:443`) | Caddy's certificate | Compose and Terraform, unless you bring your own |
| `github.com:443` | Fetching a model's commit when one is deployed from its repo | Deploys from git |
| The hosts a model declares | A run's own egress, port 443 only, through rrotor's proxy | Runs whose manifest asks for them |
| Your OpenTelemetry collector | The drain: logs, metrics, traces, audit events | `OTEL_EXPORTER_OTLP_ENDPOINT` set |
| `api.glyphh.ai:443` (`/v1/otlp/v1/…`) | Server-side logging | `RROTOR_SERVER_LOGS=on` |

Nothing connects in to rrotor except your callers on 443 (and 80 for the certificate and the redirect). The
Glyphh server never connects to it: everything it sends comes down the link rrotor opened. What crosses that link,
and what the heartbeat carries, is in [licensing.md](licensing.md).

## Licensing

Without a licence key rrotor runs as Community (Free's limits). A signed key, checked on the node with no network
request, arrives over the **link** or the **heartbeat** (a small HTTPS call every 24 hours, for networks that block
WebSockets), or, air-gapped, in `GLYPHH_LICENSE_KEY` or `/etc/rrotor/license.key` (read again hourly and on
`SIGHUP`). Each key holds for 14 days past its expiry. Where it is read, the exact heartbeat, and what is never sent,
are in [licensing.md](licensing.md).

## Upgrades

A new version is a new image tag; `/data` carries over. Pin a version in production rather than following
`latest`, and back up `/data` before you move.

- Compose: set `RROTOR_VERSION` in `.env`, then `docker compose pull && docker compose up -d`.
- Helm: `helm upgrade <release> deploy/helm/rrotor --reuse-values --set image.tag=<version>`.
- Terraform: set `rrotor_version` and apply. The VM is replaced; the data disk is detached and attached to the new
  one.

rrotor stops for the moment it is replaced: a run in flight does not survive it, as on any restart, so upgrade when
nothing is running. Go forwards only: to go back, restore the backup you took before.

## Backups

Everything rrotor keeps is on `/data`: the runtime's id and token (`runtime.json`), each model's deployed versions,
its own files and data (Ada's precedent store), each run's record and log, the parts installed, the licence held,
and, on Enterprise, the audit log (`/data/audit`).
Back that up and you can rebuild rrotor anywhere.

- **The simplest consistent copy:** stop rrotor, copy the volume, start it.
  `docker compose stop rrotor`, then `docker run --rm -v rrotor_rrotor-data:/data:ro -v "$PWD":/out debian:trixie-slim tar czf /out/rrotor-data.tar.gz -C /data .`,
  then `docker compose start rrotor`.
- **Disk snapshots:** an EBS snapshot (`data_volume_id`), a Compute Engine snapshot (`data_disk`), or a CSI
  VolumeSnapshot of the claim `data-<name>-0` (`data-rrotor-0` for a release named `rrotor`). A snapshot of a running rrotor is crash-consistent: the
  link's outbox replays what was not acknowledged, and a run that was in flight is lost.
- **A backup holds the runtime token.** Keep it as you keep the token. Restoring it on a second machine while the
  first still runs makes two of the same runtime: the newer link takes over, the older stops (`replaced`).
- **A lost token:** rotate it on the platform and put the new one in the environment; the data stays.
