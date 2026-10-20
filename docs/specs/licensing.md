# Spec: the licence key and its caps

Status: decided 2026-10-19, not built. What it replaces is built and described in
[architecture, section 9](../architecture.md#9-the-licence) and [`self-hosting/licensing.md`](../self-hosting/licensing.md);
the cluster it gates is [cluster.md](cluster.md).

rrotor runs with no key at all, as **Community**; a signed key, checked on the node with no network, lifts its
limits. The caps are enforced on the node, against its callers. Against the operator, who owns the box, they are
honest accounting rather than a lock ([Threat model](#threat-model)).

## The plans

| | Free | Pro | Team | Enterprise |
|---|---|---|---|---|
| Price | $0 | $99/month | $299/month | by contract |
| `max_nodes`: nodes in one cluster | 1 | 3 | 10 | unlimited (`null`) |
| `max_precedents`: per node, across all its models | 5,000 | 50,000 | 500,000 (TBD) | unlimited (`null`) |
| `cluster_sync_enabled` | no | yes | yes | yes |
| Failover ([cluster.md](cluster.md#4-membership-and-the-head)) | none | by restart or by hand | by restart or by hand | automatic (HA) |
| Air-gapped | no | optional | optional | yes |
| Audit log ([cluster.md](cluster.md#7-logging-audit-and-drains)) | no | no | no | yes |

**Community** is Free's limits with no key, or with a key that has stopped counting. The table is the server's
policy for what it signs; rrotor acts on the key's own numbers and on `tier`, never on the table.

## 1. The key

`<base64url payload>.<base64url signature>`, base64url without padding. The payload is UTF-8 JSON; the signature is
Ed25519 over the payload's **exact bytes** as decoded (no canonicalisation: what was signed is what is read).

| Field | Type | Meaning |
|---|---|---|
| `license_id` | UUID | This key |
| `tier` | `free` \| `pro` \| `team` \| `enterprise` | The plan; HA and the audit log follow from `enterprise` |
| `max_nodes` | int ≥ 1, or `null` | Members one cluster may have |
| `max_precedents` | int ≥ 0, or `null` | Precedents one node may hold, across its models |
| `cluster_sync_enabled` | bool | Whether the cluster transport may start |
| `issued_at`, `expires_at` | RFC 3339 UTC | Issued at a payment; expires at the end of the paid month |
| `customer_id` | string | The paying customer (the server's id, no name or address) |

Unknown fields are ignored; a missing or mistyped known field makes the key **bad**.

**The public key is in the code**, `rrotord/licence_key.py`: a tuple of raw Ed25519 public keys (base64url), the
current one and, during a rotation, the next. A key is good if any of them verifies it. `RROTOR_LICENCE_PUBLIC_KEY`
goes: an environment variable that names the key that checks the licence lets anyone who sets the environment sign
their own. Tests patch the module's tuple.

### Where it is read

In order; every one found is checked, and the best is held ([Which key is held](#which-key-is-held)).

| Source | When read |
|---|---|
| `GLYPHH_LICENSE_KEY`, alias `RROTOR_LICENCE` | At start |
| `/etc/rrotor/license.key`, alias `/data/licence.token` | At start, then every hour and on `SIGHUP`: an air-gapped operator drops next month's key in and nothing restarts |
| The heartbeat's answer, `license_key` | At boot and every 24 hours ([5](#5-the-heartbeat)) |
| The link's `welcome`, `license_key` | Each connect (the head's link, in a cluster) |
| Replication from the head | In a cluster, whenever the head holds a newer key ([cluster.md](cluster.md#5-licensing-in-a-cluster)) |
| `PUT /v1/licence` | Local mode only, as today (403 `E_LOCAL_ONLY` elsewhere) |

The key held is kept in `/data/licence.json` as `{"key": "<the key>", "source": "<where it came from>"}`, so a
restart without the environment keeps it.

### What each outcome does

| The key | rrotor | Limits |
|---|---|---|
| None anywhere | Logs once at info, `community: no licence key` | Community |
| Bad (does not parse, a field missing, the signature does not verify) | Logs at **error**, every start and every hourly re-read, `licence key refused: <why>`, with the source; `GET /v1/health` shows `licence.state: bad` | Community. Never a fatal start: a typo in a secret does not take the runtime down |
| Good, before `expires_at` | | The key's |
| Good, past `expires_at`, within 14 days | Logs a warning daily, `licence key expired <date>; Community limits from <date>`; health `grace`, `days_left` | The key's |
| Good, more than 14 days past | Logs a warning daily; health `expired` | Community |

The 14 days are rrotor's constant (`GRACE_DAYS`), not a field: a key cannot grant itself more.

### Which key is held

Of the good keys found, the one with the latest `issued_at` that is not past grace; ties go to the later
`expires_at`. Newest wins, not longest: a downgrade at the next payment takes effect, and an old higher-tier key
found in a file cannot override a newer one. When none is within grace, the newest is still held (so the health
can say what expired) and Community limits apply.

### Replacing today's licence (the migration)

One model: the key. Today's `{payload, signature}` licence (plan, limits, mods, features, `runtime_id`, `kind`,
24 hours plus `grace_days`) goes, in two releases.

1. **rrotor N** reads the key from every source above, and still takes a legacy licence wherever one arrives,
   mapped: `plan` to `tier` (unknown: `free`), `max_nodes` and `max_precedents` from the plan table above,
   `cluster_sync_enabled` from `features.sync`, `expires_at` plus its `grace_days` as `expires_at`. The server
   sends both `licence` and `license_key` in `welcome` and the heartbeat's answer; rrotor prefers the key.
2. **rrotor N+1** drops the legacy path: `rrotord/keys.py`'s `canonical`, the `runtime_id` check, `kind`, `mods`,
   `features` and `E_NOT_IN_PLAN` for an expired licence.

What changes for a caller: **an Ada model runs with no key**. Today no licence, or an expired one, refuses Ada runs
(`E_NOT_IN_PLAN`); under the key the worst case is Community's 5,000 precedents. Nothing a licence does refuses a
run. The key is not bound to a runtime (it carries no `runtime_id`): one customer's key serves each of their
clusters, and `max_nodes` bounds each cluster, not the customer ([3](#3-the-node-cap)).

`GET /v1/licence` and the health answer:

```json
{"tier": "pro", "community": false, "license_id": "…", "customer_id": "…", "source": "heartbeat",
 "issued_at": "…", "expires_at": "…", "state": "live", "days_left": 12,
 "max_nodes": 3, "max_precedents": 50000, "cluster_sync_enabled": true,
 "precedents": {"held": 1200, "cap": 50000, "over": false}}
```

## 2. The precedent cap

**The cap is per node, across all of the node's Ada models**: `max_precedents` against the sum of every Ada store's
rows (`rrotor.ada.count`, as today). In a cluster every node holds the whole synced store, so the cap is on the
synced store's size, and the head, the only writer, enforces it ([cluster.md](cluster.md#5-licensing-in-a-cluster)).

### Where it is checked

| Path | Checked | How |
|---|---|---|
| `ctx.ada.record(...)` | Yes | The SDK's wrapper (`rrotor/ada.py`, `_cap`), as today: the model's own count live, plus the others' as counted when the run started (`limits.precedents_elsewhere`) |
| A `record` whose key is already in the store | Allowed at the cap | It replaces a row and does not grow the store (the kernel's `INSERT OR REPLACE` by the spec's key); the wrapper looks the key up first |
| `ctx.ada.space.merge(path)`, the kernel's bulk union | Yes, all or nothing | The wrapper counts the rows the merge would add; past the cap the whole merge is refused |
| `learn`, `calibrate`, `refresh`, `configure`, a re-encode | Never | They add no precedent |
| A deploy | Never | No deploy brings precedents: the store lives in the model's data folder, outside git, and a re-encode keeps its rows |
| A follower applying the head's log | Never | The head checked it |

### What the caller gets

Over the cap, the wrapper raises `rrotor.ada.LimitExceeded`. A model may catch it; uncaught, the worker turns it
into a finished run, not a failed one: `Result.done` whose data is

```json
{"status": "limit_exceeded", "indexed": false,
 "message": "Precedent store capacity limit reached (50,000 max for the Pro plan). Upgrade the key to index further precedents.",
 "precedents": 50000, "cap": 50000}
```

The REST answer is 200 with that as `data`; MCP's `model_call` returns it as the tool's result, not a tool error;
the SDKs (`glyphh-ai/glyphh`) add `result.limit_exceeded`. Never a crash, never an exception to the caller, never
`failed`. `E_LIMIT_PRECEDENTS` goes. Inside a bulk loop the call ends at the first refused record; the precedents
already recorded in the run stay.

### Concurrency: the overshoot stays, bounded

Today two runs of different Ada models at once each see the other's count from when they started, and can overshoot
by what the other records meanwhile; the next run of each is refused. **Kept.** Closing it needs a counter shared
across sandboxes on the hot path of every record (a call out of the sandbox, or one SQLite file every store writes
through); the overshoot is at most what concurrent runs of *other* models record in one run, and the cap is a plan
limit, not a safety property. Runs of the same model share its store and count it live. In a cluster the head is the
only writer, so this is the same there.

### Over the cap after a downgrade

Nothing is deleted and nothing goes read-only. Every store keeps every row; queries, `facts`, `probabilities`,
`learn`, `calibrate` and settings work; records that would grow the store answer `limit_exceeded` until the total is
below the cap (by an upgrade, or by the owner removing a model or rows). `GET /v1/licence` shows `precedents.over:
true`; the daemon's nine-minute count logs a warning once per change.

## 3. The node cap

`max_nodes` is enforced as **cluster membership**: the head refuses a node's join once the cluster has
`max_nodes` members (`E_CLUSTER_FULL`), and a refused node serves nothing but `/healthz`
([cluster.md](cluster.md#4-membership-and-the-head)). A node that does not sync is not a member and is not counted.
Community is one node: `cluster_sync_enabled` is false, so the cluster transport never starts; nodes configured as
a cluster (`RROTOR_CLUSTER=on`) serve from one of them only, the rest `unlicensed`
([cluster.md](cluster.md#5-licensing-in-a-cluster)).

The server's count is separate and soft: it sees each node that heartbeats or links (`node_id`, `cluster_id`,
`role`, [5](#5-the-heartbeat)), shows "4 nodes on a 3-node plan" on the platform and may email; it never stops a
node.

## 4. Sync and HA

- **`cluster_sync_enabled: false`**, or Community: the cluster transport never starts, even with
  `RROTOR_CLUSTER=on`; rrotor logs `cluster synchronisation disabled under the Community licence` (or `under this
  licence`) at warning. One node serves, the designated head; every other answers `/readyz` 503 `unlicensed` and
  the API 503 `E_LICENCE_CLUSTER` until a key with sync arrives ([cluster.md](cluster.md#5-licensing-in-a-cluster)).
- **HA** (automatic head election) is `tier: enterprise`. Pro and Team get a head and replication, with failover
  by the head's restart or by hand ([cluster.md](cluster.md#4-membership-and-the-head)).
- **The audit log** is `tier: enterprise`.

## 5. The heartbeat

Unchanged in where and when (self-hosted with the link on, at boot and every 24 hours, `POST
{RROTOR_SERVER}/v1/license/verify`, the runtime token, fail-open). The body gains four fields:

```json
{"machine": "<64 hex>", "licence_id": "<uuid or null>", "version": "0.2.0", "active_runs": 0,
 "node_id": "rrotor-1", "cluster_id": "<uuid or null>", "role": "standalone | head | follower", "precedents": 1200}
```

The answer gains `license_key`: the newest key the server holds for the runtime's customer. A fresh key per payment
is therefore on every node within a day; a cancelled subscription gets no new key and the last one runs out (plus
grace). `node_id` is the operator's own name for the node (a pod name), not a host detail; nothing else about the
machine or its data is added.

## Threat model

- **Against callers, the caps hold.** A runtime key or a sign-in cannot record past the cap, change the key or join
  a node: the key comes from the environment, a file, the heartbeat or the head; `PUT /v1/licence` is local only;
  joining needs the cluster secret.
- **Against the operator, they are accounting.** The operator owns the box: they can patch rrotor, write rows into
  a store from their own model code with raw SQLite, or run more standalone nodes. What is worth having, and is
  cheap:
  - the key is signed, and the public key is in the code, not the environment, so a higher tier cannot be forged
    without changing rrotor itself;
  - the count is the store's rows, read from the store each time, not a counter that can be edited;
  - the daemon's own nine-minute count is reported (health, heartbeat `precedents`), so a store far past its cap
    shows on the platform;
  - newest-key-wins, so an old higher-tier key cannot be replayed over a downgrade.
- **Not attempted:** binding a key to hardware, obfuscation, refusing to run when the server is unreachable. A
  self-hosted customer who removes the checks is in breach of contract, and the server's soft counts are how that
  would be seen.

## Tests that prove it

| Test | Proves |
|---|---|
| `test_licence_key.py`: a key signed by a test key (the module's tuple patched) parses; one byte changed in the payload or signature is bad; a missing field is bad; padding and standard base64 are bad | The format |
| No key; a bad key; a good key; past expiry within 14 days; past 14 days (a fixed clock) | Community, the key's limits, grace, Community again; the log lines; never an exception at start |
| Keys from env, file, heartbeat, `welcome`: newest `issued_at` held; an older higher tier refused over a newer lower one | Which key is held |
| The file re-read every hour and on `SIGHUP` | An air-gapped renewal without a restart |
| A legacy `{payload, signature}` mapped (N); refused (N+1) | The migration |
| `RROTOR_LICENCE_PUBLIC_KEY` set to an attacker's key: their key is still refused | The public key is the code's |
| `tests/sdk/test_ada.py`: at `max_precedents` a new key gives `limit_exceeded` and `indexed: false`, the store's count unchanged; a replace at the cap goes; a merge past it adds nothing | The cap on each path |
| `test_engine.py`: a run whose record is refused ends `done` with the data above, `end` runs, the run is not `failed`; MCP's `model_call` result is not an error | What the caller gets |
| Two models, cap 10, each with 6: the second's run is refused at its first record | Across models |
| A store of 60 under a cap of 50: queries and `learn` answer, record refuses, nothing deleted | After a downgrade |
| `cluster_sync_enabled: false` with `RROTOR_CLUSTER=on`: no listener on the cluster port, the log line | Sync gated |
| A head with `max_nodes: 2` and three nodes: the third's join is `E_CLUSTER_FULL`, its `/readyz` is 503 | The node cap ([cluster.md](cluster.md#11-build-order)) |
| The heartbeat's body is exactly the fields above | Nothing else goes up |
