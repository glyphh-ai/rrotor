# Spec: clustered rrotor, logging and log drains

Status: decided 2026-10-19; build order steps 5 to 10 built (the skeleton, replication, manual failover with
membership in the log, elected heads, the aggregate drain, the packages), as
[architecture](../architecture.md#14-the-cluster) sections 13 and 14 describe. Today a self-hosted rrotor is one box ([architecture](../architecture.md));
this is how several become one runtime, and how what they log reaches the customer. The licence key and its caps,
which gate all of this, are [licensing.md](licensing.md).

The rules it keeps: the customer runs every node (Compose on a VM, Helm on Kubernetes behind a load balancer,
Terraform); Glyphh hosts nothing, relays nothing and gives no DNS name; the server is never on the data path and
sees nodes only through the link and the heartbeat; telemetry, audit and logs stay on the rrotor unless the
customer sends them somewhere.

## 1. The shape

A cluster is N rrotors of one runtime (one runtime id and token, one public host) behind the customer's load
balancer, each with its own `/data`. One is the **head**: the single writer of everything that syncs, the holder
of the cluster's link and its aggregate state, and where its drain is exported from. The rest are **followers**:
they hold every deployed version, serve the reads of programs without data locally and forward every write, and every
call of a program with data, to the head.

![The cluster](../img/cluster-topology.png)

```mermaid
flowchart LR
    Callers["Claude and apps"] -- "HTTPS :443" --> LB["the customer's load balancer<br/>TLS, /readyz"]
    LB --> H & F1 & F2
    subgraph C["one runtime, N nodes (the customer's)"]
        H["head<br/>single writer, the link,<br/>the aggregate drain"]
        F1["follower<br/>reads local"]
        F2["follower<br/>reads local"]
        F1 -- "writes forwarded" --> H
        F2 -- "writes forwarded" --> H
        H -- "replication log, snapshots" --> F1
        H -- "replication log, snapshots" --> F2
        F1 -. "telemetry" .-> H
        F2 -. "telemetry" .-> H
    end
    H -- "OTLP: logs, metrics, traces, audit" --> Col["the customer's collector"]
    H -. "opt-in server-side logging" .-> G["Glyphh server"]
    H -- "the link; heartbeat" --> G
    F1 -- "heartbeat" --> G
    F2 -- "heartbeat" --> G
```

Free (Community) is one node and has no head: everything here is off and rrotor is today's one box.

## 2. What syncs

| Syncs | What it is on `/data` | How it changes |
|---|---|---|
| **Deployed programs and versions** | `programs/<name>/versions/<commit>/` (source and venv), `active.json` | Deploy, activate, remove |
| **Runtime keys** | `access.json` | The link's `keys` (the head's link only) |
| **The licence key** | `licence.json` | The head's heartbeat or link, its file or environment |
| **Cluster state** | `cluster/state.json` (cluster id, term, members, voters), `cluster/log/` | Membership, election |

**Never syncs:** runs (`runs/<id>/`: their records, checkpoints and logs: a run lives on the node that ran it),
sandbox state and `/tmp`, parts (each node's come from its image), `runtime.json` (every node has the same id and
token from its own environment), `machine-id`, the link's outbox (the head's alone), the local audit store (each
node's own, gathered at the head, [7](#7-logging-audit-and-drains)), every program's data folder, and the vault
(`vault/`: the rrotor's key and the programs' secrets, node-local; a follower refuses to set or delete one), the
graph (`graph/`: the head's alone; a program that names it runs on the head, and a follower sends the API's graph
calls there), and
`schedules.json` (the schedules' pauses and last runs: only the head fires schedules, and a follower refuses a pause).

**No program's state replicates.** A program with `"data": true` keeps its state in its data folder on the head, and
runs on the head only: every call to it is forwarded, whatever its functions are marked (`head_only`). So does a
program that declares `secrets`: the head's vault holds them. A program without either runs its reads on the node
asked, its folders read-only. (Ada, with its precedent stores, is not a
program rrotor runs: it lives in Ada Cloud on the server.)

## 3. Replication

### The head is the single writer

Writes are forwarded to the head; the head runs them. What syncs (versions, keys, the licence, membership) changes
on the head only, which orders it in one log and ships it.

Chosen over "each node appends locally to a log the head orders" because:

- **A program's state is in one place.** A program with data writes its data folder on the head alone, so two nodes
  never hold two different states of it, and nothing has to merge them.
- **Read-your-writes.** A call that writes, then one that reads, both reach the head's data folder; with local
  state the next call through the load balancer may not see the first.
- **Latency is one LAN hop**, paid on writes and on calls of programs with data; reads of programs without data stay
  local.
- **A head outage stops writes, not local reads**, and nothing can diverge while it lasts.

### Which calls write

A manifest key, per function: `"writes": true | false`. Default `true` for a function of a program with
`"data": true`, `false` otherwise. Every call of a program with data runs on the head whatever its functions are
marked (its data folder is there alone); for a program without data, a function marked `"writes": false` runs on the
node asked and one marked `true` is forwarded.

The sandbox enforces it: on a follower a run's `/files` (and `/data`, had it one) is mounted **read-only**. A
function marked `false` that writes fails loudly (`E_READ_ONLY`, naming the function) instead of diverging.

A follower that receives a writing call (`POST /v1/runs`, `program_call`, deploy, activate, remove) forwards the
request, with its caller already authenticated, to the head over the cluster transport ([6](#6-transport)), and
answers what the head answers. Forwarding carries the caller (key hash or sign-in), the clearance and a deadline.

### What the log carries

The entry kinds: `deploy {program, version, commit_sha, manifest}` (a version made active, by deploy or activation),
`remove`, `keys {hashes}`, `licence {key}`, `member {add | remove, node}`, and `head` (a head's first entry in its
term). A program call writes no entry. An earlier release also wrote `rows` entries (an Ada store's changes); one
still in a log from before the upgrade applies as nothing.

### The log

`/data/cluster/log/` on every node: segment files of JSON lines, each entry with `seq` (one rising number per
cluster) and `term` (the head's, [4](#4-membership-and-the-head)). The head appends; a follower appends what it
receives and applies it in order. Kept until every member has applied it and a snapshot covers it, at most
`RROTOR_CLUSTER_LOG_KEEP` (1 GiB); past that a lagging follower catches up from a snapshot.

**A follower applies** each entry in order, then records `applied_seq` in `cluster/state.json`; an entry that does
not apply stops the applying there (the node goes not ready, and tries again with the next append). A `deploy`
entry makes the follower fetch the
version from the head (`GET /cluster/v2/versions/<program>/<commit>.tar`: `src/` and `venv/`, the head's build, so no
follower needs GitHub or PyPI); a node of another architecture than the head's refuses it (`E_CLUSTER_ARCH`): one
cluster, one architecture.

### Commit

| | When the head answers the call | A head lost before followers have it |
|---|---|---|
| **Pro, Team** | Once the entry is on the head's disk (asynchronous) | Lost writes are possible, of the last moments; the promotion command shows each follower's lag and refuses a lagging one without `--force` |
| **Enterprise** | Once a majority of voters hold the entry (`E_NOT_COMMITTED` 503 after 5 s, the entry kept and committed when they return) | No acknowledged write is lost |

A program call writes no entry, so its answer waits on nothing new: what it wrote is in the head's data folder, which
no follower holds. Deploys, keys and the licence are the writes this table governs.

**Read-your-writes through one node:** the head's answer carries `Rrotor-Cluster-Seq`; a follower that forwarded a
write waits (up to 2 s) until it has applied that seq before answering. A caller that moves between nodes may send
`Rrotor-Min-Seq: <seq>`; a node behind it waits up to 2 s, then forwards the read to the head.

### Catch-up

A new node, or one past the head's log: the head sends a **snapshot**: each deployed version's tar, `access.json`, the
licence and the cluster state (the seq and term it stands at, the members), as one stream; then the log
from the snapshot's seq. The node is **not ready** (`/readyz` 503) until it has applied to within one second of
the head; the load balancer sends it nothing until then.

### Runs and the node that holds them

A run lives on the node that runs it. In a cluster its id carries that node: `<node id>~<uuid>`. Any node forwards
`continue`, `cancel`, `GET /v1/runs/{id}` and its log to the holder. A holder that is gone answers `E_RUN_LOST`
(410): the run is lost as on a restart today. `GET /v1/approvals` on any node gathers every node's.

### What a caller sees when the head changes

| | Reads (`writes: false`, a program without data) | Writes, and every call of a program with data |
|---|---|---|
| Head alive | Local, every node | Forwarded, answered by the head |
| Head lost, Enterprise | Local, uninterrupted | 503 `E_NO_HEAD`, `Retry-After: 2`, for the election (1 to 3 s) |
| Head lost, Pro and Team | Local, uninterrupted | 503 `E_NO_HEAD` until the head restarts or is promoted by hand |
| A run on the lost head | `E_RUN_LOST` | `E_RUN_LOST` |

The SDKs retry `E_NO_HEAD` with backoff for up to 10 s. MCP returns it as a tool error, which Claude retries.
`E_NO_HEAD` means nothing ran (no head, or the head refused the request first). A write the head had in hand when it
was lost (deposed during the call, the connection closed with the forward in flight, a writing run stopped as it
stepped down) is 503 `E_HEAD_LOST`, with no `Retry-After`: it may have been applied, so the SDKs never retry it.

## 4. Membership and the head

### Finding each other

| Setting | Where |
|---|---|
| `RROTOR_CLUSTER_PEERS=rrotor-0:7778,rrotor-1:7778,rrotor-2:7778` | Compose, Terraform: a static list, the same on every node |
| `RROTOR_CLUSTER_DNS=rrotor-headless.rrotor.svc.cluster.local` | Kubernetes: the StatefulSet's headless Service (`publishNotReadyAddresses: true`); rrotor resolves it every 10 s and dials every address on `RROTOR_CLUSTER_PORT` |

The list or the DNS answer is only how nodes **find** each other. **Membership** is in the log: a node is a member
once the head has committed its `member add`. Each node has an id (`RROTOR_NODE_ID`, default the hostname: the pod
name `rrotor-0`), kept in `cluster/state.json`; two nodes with one id are refused.

### Joining, and `max_nodes`

A node with `RROTOR_CLUSTER=on` and a licence with `cluster_sync_enabled` dials its peers, finds the head (any
member answers who it is) and asks to join. The head admits it unless the cluster has `max_nodes` members
(`E_CLUSTER_FULL`), commits `member add`, and sends it a snapshot. A refused node logs it at error, keeps its
`/data`, answers 503 `E_CLUSTER_FULL` to everything but `/healthz` (so `/readyz` keeps it out of the load balancer)
and asks again every minute: an upgrade or a removed member lets it in. Leaving is `rrotord cluster leave <node>`
(or a member unseen for `RROTOR_CLUSTER_EVICT_AFTER`, default 24 h, removed by the head). Changes go one node at a
time.

**Bootstrap:** the first time, with no member state anywhere, the node named by `RROTOR_CLUSTER_HEAD` (default:
the lowest node id among the peers) makes the cluster: a new `cluster_id` (UUID), term 1, itself head. Every other
node waits for it.

### The election: terms, a hand-written Raft, no external dependency

| Option | For | Against |
|---|---|---|
| A lease in an external store (etcd, Redis, a database) with fencing tokens | Simple to write | A dependency Compose does not have, and the lease is only as safe as that store |
| A Kubernetes Lease object | Native on Kubernetes | Kubernetes only; RBAC and a client in the image; Compose and Terraform still need something else |
| A Raft library (PySyncObj and the like) | Election written already | Its own TCP transport and serialisation (pickle, in PySyncObj), its own threads; neither aiohttp nor asyncio nor our mTLS; its log is not ours |
| **Raft's election and log rules, in `rrotord/cluster/`, on our own transport and log** | One mechanism everywhere; terms are the fencing tokens; the log above is already Raft's shape (term, seq) | We own its correctness: a deterministic simulation test (below) |

**Chosen: the last.** What it is, exactly: Raft's terms, `RequestVote` with the log-up-to-date rule, `AppendEntries`
with (term, seq) matching and truncation of a diverged tail, commit by majority of voters (Enterprise), snapshots,
single-node membership changes. **Voters** are the first five members (by join); the rest are non-voting learners,
so a large Enterprise cluster's commits wait on five, not on all.

**Fencing.** Every entry, forward and apply carries the term. A node refuses anything from a lower term; a head
that sees a higher term steps down at once, fails its in-flight writing runs (`E_HEAD_LOST`) and, on rejoining,
discards any entries past the new head's log for the programs they touched by taking those programs' snapshot.

**Split brain.** Enterprise: a head that has not heard from a majority of voters within the election timeout
(1.5 s) stops accepting writes before any other node can be elected (timeouts 1.5 to 3 s, randomised; heartbeats
every 250 ms). A partition's minority side serves reads and refuses writes. Pro and Team: there is no automatic
election, so there is never a second head unless an operator promotes one; `rrotord cluster promote` requires a
majority of members reachable and asks the operator to confirm the old head is down when it is unreachable (its
higher term then fences the old head when it returns).

### What HA means, by tier

| | Head | When the head is lost | Writes need |
|---|---|---|---|
| **Pro, Team** | Fixed: the bootstrap head, until moved | The cluster serves reads; writes wait for the head to restart (Kubernetes and `restart: unless-stopped` do this in seconds) or for `rrotord cluster promote <node>` | The head |
| **Enterprise** | Elected | Another voter is elected in 1.5 to 3 s; writes resume | A majority of voters |

An Enterprise cluster of two nodes behaves like Pro (no majority survives a loss); three or more voters are
recommended, an odd number.

### The head's other jobs

It holds the runtime's **link** (followers do not link: there is one link per runtime, as today, so a follower
never takes it over); server-sent deploys arrive there. It gathers the followers' `usage` key counts once a minute
and sends one `usage`. It exports the cluster's **drain** ([7](#7-logging-audit-and-drains)) and keeps the cluster's
audit store. It answers `GET /v1/cluster`. A new head takes all of these over; the link reconnects from it.

## 5. Licensing in a cluster

- **One key per cluster.** Each node is started with the same key (the packages put it in the cluster's Secret),
  and the head propagates the newest it holds (from its heartbeat or link) as a `licence` entry. **Every node checks
  the signature itself**; a node never takes the head's word for a key.
- **The head checks the limits:** `max_nodes` at join, `cluster_sync_enabled` before anything replicates. A key's
  `max_precedents` is Ada Cloud's and is not read ([licensing.md](licensing.md)).
- **A downgrade:** a key with a smaller `max_nodes` makes the head remove the members joined last until the cluster
  fits (never the head); they become refused nodes, `/data` kept.
- **Past grace, or Community:** sync stops. The head runs on as a standalone node with Community limits; the
  followers become `unlicensed` (`E_LICENCE_CLUSTER`), their data kept at their applied seq. A new key resumes the
  cluster from there.
- **An unlicensed cluster serves from one node, never from divergent stores** (built). Nodes configured as a cluster
  (`RROTOR_CLUSTER=on`, behind one load balancer) with no key that allows sync would otherwise each serve their own
  store. Exactly one serves, the designated head: the cluster's head as the node last knew it; else
  `RROTOR_CLUSTER_HEAD`; else the lowest node id among the peers (an address's host or first label; an IP address
  stands for itself, matched against the node's advertised address). Every other node is `unlicensed`: `/readyz` 503
  (`unlicensed`, `E_LICENCE_CLUSTER`), the API 503 `E_LICENCE_CLUSTER` ("This cluster has no licence key with sync:
  only `<head>` serves; add a Pro, Team or Enterprise key.") but for `/healthz`, `/readyz`, the health and `GET
  /v1/cluster`, no link, `/data` kept, logged at error once. The listener still never starts; the key files are read
  again every 10 s while the gate is closed, so a key with sync dropped in clusters the nodes.
- **Every node heartbeats** with its `node_id`, `cluster_id` and `role` ([licensing.md](licensing.md#5-the-heartbeat)),
  so the server sees every node, synced or not.

## 6. Transport

| | gRPC | **WebSocket on aiohttp** |
|---|---|---|
| Dependencies | `grpcio` and `protobuf`, about 30 MB in the image per architecture, a code generator, stubs for mypy | None new: aiohttp is the server and the link's client already |
| Patterns in the code | New: a second server stack, its own asyncio integration | The link: JSON frames, `type` and `id`, `ack`, `ping`; the outbox; reconnect with backoff |
| Streams | Native | One long-lived WebSocket per pair of nodes, binary frames for snapshots and tars |
| mTLS | Yes | Yes (`ssl.SSLContext` with `CERT_REQUIRED` on both sides) |
| HTTP/2 | Yes | No (aiohttp's server is HTTP/1.1); not needed: one connection per pair, multiplexed by message ids |

**Chosen: WebSocket on aiohttp**, on its own listener, `:7778` (`RROTOR_CLUSTER_PORT`), never on 7777 and never
behind the public load balancer. Messages: `hello`, `vote`, `append` (entries), `ack {seq}`, `snapshot` (binary
chunks), `forward {request}`, `answer`, `telemetry` (OTLP batches, [7](#7-logging-audit-and-drains)), `ping`.
`GET /cluster/v2/versions/…` is a plain request on the same listener. The `v2` in every path is the cluster's wire
and log format (v1 named a program `model`): a node never misreads one of another release. Dialling a node with no
`/cluster/v2/link` is `E_CLUSTER_VERSION`, and any `/cluster/v1/` request is answered 426 `E_CLUSTER_VERSION`, so
every node of a cluster is upgraded to the same release.

**The cluster's credentials: one secret, mTLS from it.** `RROTOR_CLUSTER_SECRET` (32 bytes or more, base64) is the
only thing the operator makes. From it every node derives, with HKDF-SHA256, the same Ed25519 **cluster CA** key
and a fixed CA certificate; at start each node makes its own key and issues itself a leaf certificate (its node id
in the SAN) from that CA, with `cryptography` (a dependency already). Both ends require a certificate from the
cluster CA and check the peer's node id. So: mutual TLS between every pair, no certificate to issue or rotate by
hand, and rotation is a new secret, rolled node by node with `RROTOR_CLUSTER_SECRET_PREVIOUS` accepted meanwhile.
Anyone with the secret can join (up to `max_nodes`), which is the point of it: keep it as the runtime token is
kept. **Never a runtime key:** runtime keys are callers' credentials; a node-to-node request carries the caller's
identity as data, inside the cluster's own TLS.

## 7. Logging, audit and drains

**Everything stays on the rrotor by default.** Nothing below leaves it unless the customer sets a drain (their
collector) or turns on server-side logging (Glyphh). The heartbeat and the link's `usage` are licensing, not
telemetry, and keep their fixed fields.

### What rrotor logs

JSON lines on stdout, as today (`rrotord/log.py`: `at`, `level`, `logger`, `message`, fields; tokens masked),
level `RROTOR_LOG_LEVEL`. Added to every line: `node_id`, and `cluster_id` and `role` in a cluster; `run_id`,
`program`, `version`, `function` where there is one; `trace_id` and `span_id` when tracing. Kept locally by the
container runtime: the packages set Docker's `json-file` driver to `max-size: 10m`, `max-file: 5`; on Kubernetes the
kubelet rotates. A run's own `ctx.log` lines stay in `runs/<id>/log.jsonl` as today.

### Audit events (Enterprise)

One per thing that happened, who did it and how it ended:

```json
{"id": "<uuid>", "at": "2026-10-19T09:00:00Z", "node_id": "rrotor-1", "cluster_id": "…",
 "kind": "run.call", "actor": {"type": "key", "key_sha256": "3f2a…9c"},
 "client": "10.0.4.17", "program": "incidents", "version": 3, "function": "record", "run_id": "rrotor-0~…",
 "outcome": {"status": "done", "code": null}, "seconds": 0.21}
```

| `kind` | When |
|---|---|
| `run.call`, `run.continue`, `run.cancel`, `run.end` | Every call, by any route (REST, MCP, the link); `run.end` with the final status |
| `program.deploy`, `program.activate`, `program.remove`, `program.deploy_failed` | Every deploy and its outcome |
| `keys.changed` | The runtime keys' list changed (counts and the hashes added and removed) |
| `licence.changed`, `licence.expired`, `licence.bad` | A key taken, past grace, refused |
| `cluster.join`, `cluster.refused`, `cluster.leave`, `cluster.head` | Membership and head changes, with the term |
| `access.denied` | A 401 or 403, with the code |
| `config.server_logs` | Server-side logging turned on or off |

`actor.type` is `key` (its SHA-256, never the key), `sign-in` (the subject the server's introspection names), `local`,
`server` (a link `deploy`) or `rrotor` (its own work, a licence key read from its environment). `client` is the request's address (the last
`X-Forwarded-For` hop the packages' proxy sets), kept locally and in the customer's drain only.

**Kept** in `/data/audit/<YYYY-MM-DD>.jsonl`, one file a day, appended and fsynced per event; deleted past
`RROTOR_AUDIT_RETENTION_DAYS` (90) or when the folder passes `RROTOR_AUDIT_MAX_MB` (1024), oldest first, which
itself is logged. In a cluster each node keeps its own and sends its events to the head, which keeps the cluster's
in the same form under `/data/audit/cluster/`. `GET /v1/audit` reads the head's. Below Enterprise there is no audit
store: the same events are ordinary log lines (`logger: rrotor.audit`) and are not kept by rrotor.

### Drains: OpenTelemetry to the customer's collector

The recommended setup: each rrotor (or the head, for the cluster) exports logs, metrics, traces and audit events by
**OTLP** to the customer's own collector, never through Glyphh.

- **The protocol:** OTLP over HTTP with protobuf (`http/protobuf`), with the OpenTelemetry Python SDK and its
  `opentelemetry-exporter-otlp-proto-http` exporter (pure Python, a few MB). OTLP over gRPC is not offered: it needs
  `grpcio` (see [6](#6-transport)); a collector takes HTTP and forwards gRPC if the backend wants it.
  `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` is refused at start (`E_CONFIG`).
- **The settings are OpenTelemetry's own:** `OTEL_EXPORTER_OTLP_ENDPOINT` (and the per-signal `…_LOGS_ENDPOINT`,
  `…_METRICS_ENDPOINT`, `…_TRACES_ENDPOINT`), `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_EXPORTER_OTLP_CERTIFICATE`,
  `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_SERVICE_NAME` (default `rrotor`), `OTEL_TRACES_SAMPLER` and
  `OTEL_TRACES_SAMPLER_ARG` (default `parentbased_always_on`). No endpoint, no drain. `RROTOR_DRAIN_SIGNALS`
  (default `logs,metrics,traces,audit`) picks what goes.
- **Resource attributes** rrotor sets: `service.name`, `service.version`, `service.instance.id` (the node id),
  `rrotor.runtime.id`, `rrotor.cluster.id`, `rrotor.node.role`, `rrotor.mode`.
- **Logs:** every line above as an OTLP log record. **Audit events:** OTLP log records with
  `event.name = rrotor.audit.<kind>` and the event's fields as attributes. **Metrics:** `rrotor.runs` (counter: program,
  function, status), `rrotor.run.seconds` (histogram),
  `rrotor.link.state`, `rrotor.cluster.lag` (seqs behind the head, per follower), `rrotor.cluster.term`,
  `rrotor.drain.dropped`. **Traces:** a span per API request; child spans for the forward to the head, the sandbox's
  start, the function, the commit wait; the caller's `traceparent` honoured.
- **Per node or at the head:** `RROTOR_DRAIN_AT=head` (the default in a cluster): followers send their OTLP batches
  to the head over the cluster transport and the head exports them as they are (each keeps its own
  `service.instance.id`), so the collector sees one endpoint and the customer opens one egress. `node`: each node
  exports itself. A follower that cannot reach the head buffers, as below, and sends when it can.
- **Buffer and drop:** logs, metrics and traces wait in memory, up to `RROTOR_DRAIN_BUFFER` records (10,000) per
  signal, sent in batches every 5 s with retry and backoff; when full the **oldest** are dropped, counted in
  `rrotor.drain.dropped` and logged once a minute. **Audit events are never dropped from the drain:** the export
  reads the audit files from a cursor (`/data/audit/.cursor`) and resumes there, so an endpoint that is down for a
  day gets the day's events when it is back, as long as retention still holds them. Below Enterprise the audit
  events are log records and follow the log rule.

### Opt-in server-side logging

`RROTOR_SERVER_LOGS=on` (default `off`) sends the same OTLP signals, in addition to any drain, to
`{RROTOR_SERVER}/v1/otlp/v1/{logs,metrics,traces}` with the runtime token, so the platform can show them. At every
start and in `GET /v1/health` rrotor says plainly:

> Server-side logging is on: this rrotor's logs, metrics, traces and audit events are sent to Glyphh
> (api.glyphh.ai) and leave your infrastructure. Set RROTOR_SERVER_LOGS=off to keep them here.

It is refused with the link off (`E_CONFIG`: an air-gapped rrotor sends nothing). It never sends what the next
section keeps back, and never `client` addresses, even when the customer's own drain gets them. In a cluster the
head sends it for the cluster.

### Redaction

Never in a log line, audit event, span or metric: a program's inputs, its outputs (`Result.data`, prompts, answers),
`ctx.log`'s fields, secrets, tokens or keys (keys as SHA-256 only), the bodies of requests. Run ids, program names,
function names, statuses, codes, durations and counts are what is sent. `RROTOR_LOG_PAYLOADS=on` (default `off`)
adds inputs and `Result.data` to the run's span attributes and `run.end` audit events, truncated to 4 KB each, **for
the customer's drain only**; server-side logging never carries them. The existing token mask stays on everything.

## 8. Programs and the gateway, later

**Programs** (coding-agent CLIs run on the runtime) will be scheduled from the head: it is the one place that knows
every node's load and which node holds what. What this design leaves for it: the head's job list
([4](#4-membership-and-the-head)) is a registry new jobs join; members report their free CPU, memory and running
runs in `ping`; the `forward` message can name a target node, not only the head; a run's id names its holder, so a
Program's session can live on one node and be reached from any. No placement, queue or Program state is designed
here.

**The gateway** (programs behind OpenAI- and Anthropic-compatible endpoints) will be configured once at the head and
replicated as its own entry kind, then served on every node like any read. What this leaves for it: the log's entry
kinds are open (a node refuses a kind it does not know only by failing readiness, so an upgrade rolls out before
the head writes new kinds); the forward path and the read path are already separate, so a gateway route can be
local or head-only per route.

## 9. Failure cases

| Case | What happens |
|---|---|
| The head dies mid-write | The run is lost with it; the caller gets a dropped connection or `E_HEAD_LOST` (never retried: it may have been applied). Enterprise: an uncommitted entry is gone from the cluster; the old head discards it on rejoin (snapshot of the programs it touched). Pro and Team: entries the followers did not get are lost if another node is promoted; kept if the head restarts |
| The head dies between runs | Enterprise: a new head in 1.5 to 3 s. Pro and Team: writes 503 until it restarts or is promoted |
| A network partition | Enterprise: the majority side elects (or keeps) a head and takes writes; the minority side serves reads at its last applied seq and refuses writes; on healing the minority catches up. Pro and Team: the side without the head serves reads only |
| A follower falls behind or restarts | Catches up from the log, or from a snapshot past `RROTOR_CLUSTER_LOG_KEEP`; not ready until within a second of the head |
| A node past `max_nodes` | Refused at join (`E_CLUSTER_FULL`), not ready, data kept, asks again every minute |
| Two nodes with one node id | The second is refused (`E_CLUSTER_NODE_ID`) |
| A node of another architecture | Refuses the first `deploy` entry (`E_CLUSTER_ARCH`), not ready |
| The cluster secret differs on one node | Its TLS handshakes fail; it logs `cluster: peer certificate not from this cluster` and stays not ready |
| The licence expires mid-run | Runs in flight finish. Within 14 days nothing changes. Past grace: sync stops, the head runs alone on Community limits, followers are `unlicensed`, out of the load balancer (data kept) |
| Nodes configured as a cluster without a key that allows sync (none, Free, no `cluster_sync_enabled`, past grace) | No listener. One node serves, the designated head (the cluster's head as last known, else `RROTOR_CLUSTER_HEAD`, else the lowest node id); every other answers `/readyz` 503 `unlicensed` and the API 503 `E_LICENCE_CLUSTER` naming it, data kept, no link. A key with sync arriving clusters them; never two stores behind one load balancer |
| A function marked `"writes": false` writes | `E_READ_ONLY` on a follower; its folders are untouched |
| The disk fills on a follower | Apply fails; the node stops applying, goes not ready and logs it; the head keeps its entries until it returns or is evicted |
| The drain's endpoint is down | Logs, metrics, traces buffered to `RROTOR_DRAIN_BUFFER` per signal, oldest dropped and counted; audit events resume from the cursor when it is back |
| The head is down, `RROTOR_DRAIN_AT=head` | Followers buffer as above; the new head (or the old one, restarted) takes the batches |
| Glyphh unreachable | Nothing in the cluster changes: no part of replication, election or drains uses the server. The key holds to its expiry plus grace |

## 10. Settings and interfaces

### Settings

Every new variable (the licence's are in [licensing.md](licensing.md#where-it-is-read)). A setting missing or
malformed stops rrotor at start with `E_CONFIG`, naming it.

| Variable | Default | Meaning |
|---|---|---|
| `RROTOR_CLUSTER` | `off` | `on`: join or make a cluster (needs a key with `cluster_sync_enabled`) |
| `RROTOR_NODE_ID` | the hostname | This node's name in the cluster: `[a-z0-9-]{1,63}` |
| `RROTOR_CLUSTER_PEERS` | | `host:port,…`: where the members are (Compose, Terraform) |
| `RROTOR_CLUSTER_DNS` | | A name whose A/AAAA records are the members (Kubernetes' headless Service); one of this and `PEERS` |
| `RROTOR_CLUSTER_PORT` | `7778` | The cluster listener |
| `RROTOR_CLUSTER_ADVERTISE` | `<node id>:<port>` | How the other nodes reach this one |
| `RROTOR_CLUSTER_SECRET` | | Required with the cluster on: 32 or more bytes, base64 |
| `RROTOR_CLUSTER_SECRET_PREVIOUS` | | Accepted too, while a new secret rolls out |
| `RROTOR_CLUSTER_HEAD` | the lowest node id | Which node makes the cluster at bootstrap |
| `RROTOR_CLUSTER_LOG_KEEP` | `1g` | The log kept for catch-up before a snapshot is needed |
| `RROTOR_CLUSTER_EVICT_AFTER` | `24h` | A member unseen this long is removed by the head |
| `RROTOR_AUDIT_RETENTION_DAYS` | `90` | Days of audit kept (Enterprise) |
| `RROTOR_AUDIT_MAX_MB` | `1024` | Size of the audit folder, oldest deleted first |
| `RROTOR_DRAIN_SIGNALS` | `logs,metrics,traces,audit` | What the drain sends |
| `RROTOR_DRAIN_AT` | `head` in a cluster | `head` or `node` |
| `RROTOR_DRAIN_BUFFER` | `10000` | Records held per signal while the endpoint is down |
| `RROTOR_SERVER_LOGS` | `off` | `on`: also send them to Glyphh, with the warning above |
| `RROTOR_LOG_PAYLOADS` | `off` | `on`: inputs and results in the customer's drain |
| `OTEL_EXPORTER_OTLP_*`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_SERVICE_NAME`, `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | | The drain, as OpenTelemetry defines them |

### API and MCP

| Call | Does |
|---|---|
| `GET /readyz` (open) | 200 when this node may take traffic: standalone and up; or a member applied to within a second of the head. 503 with the reason otherwise (`joining`, `catching_up`, `refused`, `no_cluster`, `unlicensed`) |
| `GET /v1/cluster` | `{cluster_id, node_id, role, head, term, commit_seq, applied_seq, tier, max_nodes, ha, members: [{node_id, address, role, voter, applied_seq, lag, last_seen, version}], refused: [{node_id, at, code}]}`; standalone: `{role: "standalone"}`; the cluster on without a key that allows sync: `{role: "standalone", node_id, status: "standalone" \| "unlicensed", reason, serving, refusal}` |
| `GET /v1/audit?since&until&kind&actor&program&run_id&limit&cursor` | The cluster's audit events, newest first, paged; on a follower, answered by the head; Enterprise only (403 `E_NOT_IN_PLAN` otherwise) |
| `GET /v1/health` | Adds `cluster` (as above, short), `drains` (per signal: `state`, `buffered`, `dropped`, `last_export_at`), `server_logs` (on or off, with the warning), the licence as [licensing.md](licensing.md#replacing-todays-licence-the-migration) shows it |
| MCP `cluster_status` | `GET /v1/cluster` |
| MCP `audit_query` `{since?, until?, kind?, program?, run_id?, limit?}` | `GET /v1/audit` |

Not on the API, on purpose: joining, leaving and promoting. They are `rrotord cluster join | leave <node> | promote
<node> [--force]`, run on a node (`docker compose exec`, `kubectl exec`) and authenticated by the cluster secret,
so no runtime key can change the cluster.

### The link and the heartbeat

- **`hello`** gains `node_id`, `cluster_id` and `role`. Only a standalone node or a head opens the link. The
  server's `welcome` and the heartbeat's answer gain `license_key`.
- **`usage`** from the head carries the cluster's key counts (summed from the followers each minute).
- **The heartbeat**, from every node: `node_id`, `cluster_id`, `role` ([licensing.md](licensing.md#5-the-heartbeat)).
- **The server** needs: `license_key` in `welcome` and the heartbeat's answer; the heartbeat's new fields stored per
  node (`node_id` within the runtime) and shown; a subject in the introspection answer for audit (`sub`); the
  OTLP receiver for server-side logging.

## 11. Build order

Each step ships on its own, keeps 100% coverage, and leaves a standalone rrotor working.

| # | Step | Tests | Proves |
|---|---|---|---|
| 1 | **The licence key** ([licensing.md](licensing.md)): the format, the embedded public key, every source, Community, grace, newest-wins, hourly re-read and `SIGHUP`, the heartbeat's `license_key`, the legacy mapping | `test_licence_key.py`, `test_heartbeat.py`, `test_daemon.py` | A node runs on no key, a bad key, a good key and an expired one, never crashing |
| 2 | **The precedent cap per node**: built, then removed with Ada (Ada and its limits live in Ada Cloud) | | |
| 3 | **Structured logs and audit events**: the fields, the audit store, rotation and retention, `GET /v1/audit`, `audit_query`, Enterprise-gated | `test_audit.py`, routes, MCP | Who called what is recorded, kept and queried locally; nothing redacted leaks (a test feeds secrets and payloads and greps every output) |
| 4 | **Drains**: the OTel SDK, OTLP over HTTP, the resource attributes, buffer and drop, the audit cursor, `RROTOR_SERVER_LOGS` with its warning | `test_drain.py` against an in-process fake collector | Signals reach a collector; a down endpoint loses only the oldest logs and no audit event |
| 5 | **The cluster's skeleton**: settings, node id, the `:7778` listener, the derived CA and mTLS, `hello`, peers by list and DNS, bootstrap, join with `max_nodes`, `/readyz`, `GET /v1/cluster`, `cluster_status`, sync gated by the key. Nothing replicates yet | `tests/daemon/cluster/` with N daemons in one event loop | Nodes find and authenticate each other; a foreign secret is refused; the node past `max_nodes` is refused; Community never listens |
| 6 | **Replication with a fixed head**: the `writes` manifest key, read-only follower runs, programs with data on the head only, forwarding, run-id holders, the log, apply, `Rrotor-Cluster-Seq`, snapshots and catch-up, `deploy`, `keys` and `licence` entries, the head-only link and summed `usage` (the Ada store's change capture, built here, went with Ada) | Two and three daemons with fake sandboxes | A write on any node runs on the head; a read stays local; a new node catches up; a follower never writes |
| 7 | **Manual failover (Pro, Team)**: terms and fencing, `rrotord cluster promote`, lag shown, `--force`, a rejoining old head discarding its tail | The simulation (below) | Writes resume on the promoted node; no node accepts a lower term |
| 8 | **Elected heads (Enterprise)**: votes, timeouts, majority commit, voters and learners, `E_NOT_COMMITTED`, `E_NO_HEAD` with `Retry-After`, SDK retries | A deterministic simulation: a seeded scheduler over in-memory transports, crashes, partitions, delays and restarts across thousands of seeds; it checks one head per term, no committed entry lost, every replica equal at the end | Raft's safety on our transport, not by argument |
| 9 | **The aggregate drain**: followers' `telemetry` to the head, `RROTOR_DRAIN_AT`, the cluster audit store | Three daemons and a fake collector | One endpoint sees every node's signals with each node's identity |
| 10 | **The packages** (below) and the self-hosting docs | CI's `deploy` job: `helm template` with `cluster.enabled`, Compose `config`, `terraform validate` | They render; a three-node Compose cluster comes up in the image job |

Outside this repo, before step 1: `license_key` from the server. Before step 4's
server-side logging: the server's OTLP receiver.

## 12. The packages

- **Helm:** `cluster.enabled`, `replicas` (default 3; refused above the key's `max_nodes` only by rrotor, not by
  the chart), `cluster.secret` or `cluster.existingSecret` (the chart generates one with `randAlphaNum` on install
  and keeps it with `lookup`). The StatefulSet with `podManagementPolicy: Parallel`, a pod anti-affinity across
  nodes and zones, the readiness probe on `/readyz`, the liveness on `/healthz`, `RROTOR_NODE_ID` from
  `metadata.name`, `RROTOR_CLUSTER_DNS` set to the headless Service, which gains port `cluster: 7778` and
  `publishNotReadyAddresses: true`. The Service and Ingress reach every ready pod. The PDB becomes
  `maxUnavailable: 1`. A NetworkPolicy allows 7778 between the release's pods only. The chart's "one replica" rule
  goes, and the "not scaled" note becomes the cluster's.
- **Compose:** `docker-compose.cluster.yml`, one machine, three services `rrotor-0..2` (each its own volume, the
  same `.env` plus its `RROTOR_NODE_ID`, `RROTOR_CLUSTER_PEERS` naming the three) and Caddy load-balancing over them
  with a health check on `/readyz`: for trying a cluster, not for HA. For HA, `docker-compose.node.yml` on each of N
  machines, `RROTOR_CLUSTER_PEERS` naming their private addresses, 7778 open between them only, a load balancer in
  front.
- **Terraform:** `node_count` (default 1; 3 for a cluster) VMs across zones, each running the node Compose file;
  the cluster secret in AWS Secrets Manager or Google Secret Manager; a security group or firewall rule for 7778
  between the nodes only; with `node_count` above 1, a load balancer (an AWS NLB with ACM, or Google's external
  HTTPS load balancer with a managed certificate) health-checking `/readyz`, and Caddy dropped from the VMs.
- **Ports:** 7777 (the API, to the load balancer), 7778 (the cluster, node to node only, never public).
- **Secrets the operator holds:** the runtime token (as today), the licence key, the cluster secret.

## 13. Open questions for Chris

1. Should a key be bound to one runtime (a `runtime_id` in the payload)? Unbound, one customer's key serves any
   number of clusters, each held to `max_nodes`; bound, a key per runtime and more for the server to issue.
2. (Closed: Team's precedents per node were Ada's, now Ada Cloud's, not rrotor's.)
3. HA as defined here: Pro and Team fail over by restart or by hand, Enterprise automatically. Or automatic for
   Team too?
4. Is the audit log Enterprise only, as decided, while drains (of logs, metrics and traces) are on every plan,
   Free included?
5. Air-gapped Pro and Team: monthly keys mean pasting a key each month. Issue air-gapped customers a key for the
   term they paid (a year), or keep monthly?
6. Server-side logging: on every plan, or only where the platform can show it (Pro and up)? And its retention on
   the server.
7. OTLP over gRPC: is "use a collector" acceptable for customers whose backend wants gRPC, against about 30 MB of
   `grpcio` in the image?
