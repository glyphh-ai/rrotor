# Licensing a self-hosted rrotor

Written for the people who review what a piece of software sends out of their network. In short: rrotor runs
without a licence key at Community's limits; it checks a signed key on the machine, with no network request; it
gets the next key over the link or a small daily HTTPS call whose whole body is shown below; and an air-gapped
rrotor never calls out at all.

## The licence key

rrotor runs without one, as **Community**: one node, 5,000 precedents across its Ada models, no cluster sync. A
licence key lifts those limits to the ones it carries.

A key is `<payload>.<signature>`, each base64url without padding. The payload is JSON; the signature is Ed25519
over its exact bytes, made by the Glyphh server. rrotor checks it with Glyphh's public key, which is in rrotor's code
(`rrotord/licence_key.py`), not in the environment: checking needs no network, a key changed by one byte is refused,
and setting an environment variable cannot make rrotor trust another signer.

| Field | What it says |
|---|---|
| `license_id` | This key's id (a UUID) |
| `tier` | `free`, `pro`, `team` or `enterprise` |
| `max_nodes` | Nodes one cluster may have (null: unlimited) |
| `max_precedents` | Precedents one node may hold, across its Ada models (null: unlimited) |
| `cluster_sync_enabled` | Whether the nodes of a cluster may sync |
| `issued_at`, `expires_at` | When it was issued (at a payment) and when it stops being current (UTC) |
| `customer_id` | The paying customer, as the server's id: no name or address |

Nothing else is in it. You can read any key you hold:

```sh
printf %s "$GLYPHH_LICENSE_KEY" | cut -d. -f1 | tr '_-' '/+' | base64 -d 2>/dev/null | python3 -m json.tool
```

`GET /v1/licence` and `GET /v1/health` show the key held, where it came from, its state (`live`, `grace`,
`expired`, `bad`, or `none`) with the days left, the limits that apply, and the node's precedents against its cap
(`over: true` after a downgrade: nothing is deleted, growth is refused).

## Where rrotor reads it

| Source | When |
|---|---|
| `GLYPHH_LICENSE_KEY` (or `RROTOR_LICENCE`) | At start |
| The file `/etc/rrotor/license.key` (or `/data/licence.token`) | At start, every hour, and on `SIGHUP` |
| The heartbeat's answer | At boot and every 24 hours, with the link on |
| The link's `welcome` | Each time the link connects |

Every key found is checked. Of the good ones rrotor holds the one **issued last** that is not more than 14 days
past its expiry, so a change of plan at the next payment takes effect and an older key left in a file cannot
override a newer one. The key held is kept in `/data/licence.json`, so a restart without the variable keeps it.

A key that does not check out is logged at error, with where it came from, at every start and every hourly
re-read (`licence key refused: <why>`), and rrotor runs as Community: a typo in a secret never stops it.

**This release** still takes the licence of earlier releases (`{payload, signature}`, or its offline token)
wherever one arrives, with its plan's limits; a key is preferred to it. The next release takes keys only.

## The heartbeat, exactly

At boot and then every 24 hours:

```http
POST /v1/license/verify HTTP/1.1
Host: api.glyphh.ai
Authorization: Bearer <runtime token>
Content-Type: application/json

{"machine": "<64 hex characters>", "licence_id": "<uuid or null>", "version": "<rrotor version>", "active_runs": <int>,
 "node_id": "<this node's name>", "cluster_id": null, "role": "standalone", "precedents": <int or null>}
```

| Field | What it is | Why |
|---|---|---|
| `machine` | SHA-256 (hex) of `<runtime id>:<machine id>`, where the machine id is the host's `/etc/machine-id` when the container can read one, else a random id rrotor makes once and keeps in `/data` | To tell apart two machines running the same runtime. It is a hash: the machine id cannot be read back from it |
| `licence_id` | The id of the key rrotor holds now (`license_id`), or `null` | To see which key is in use |
| `version` | rrotor's version, e.g. `0.1.0` | To show it on the platform |
| `active_runs` | How many runs are in progress, a number | A count only, nothing about the runs |
| `node_id` | `RROTOR_NODE_ID`, else the container's hostname: your own name for the node | To count your nodes |
| `cluster_id`, `role` | `null` and `standalone` (clusters are not built yet) | To count your nodes |
| `precedents` | How many precedents the node holds across its Ada models, a number | To show use against the key's cap |

The answer is `200 {"license_key": "<key>"}`: the newest key the server holds for your subscription (this release
also takes the earlier `licence` beside it). If the call fails or is refused, rrotor keeps the key it has and tries
again.

**Never sent, in the heartbeat or anywhere it is stored:** IP addresses, hostnames, prompts, model inputs or
outputs, model code, files, secrets, or anything about your users. The server keeps only what the body
carries and when it came. It does not store or log the caller's address
for this route. As with any HTTPS request, the address is visible on the network path itself (your egress proxy,
the edge of Glyphh's hosting provider); it is not in the request and the server does not keep it.

## The link, when it is on

The link is the same as a hosted rrotor's ([architecture, section 6](../architecture.md#6-the-link-protocol-1)):
one WebSocket rrotor opens to the server, authenticated with the runtime token; the server never connects in. What
goes up it:

| Message | Carries |
|---|---|
| `hello` | The protocol, rrotor's version, its mode, the runtime id, the model versions deployed |
| `deployed` | After each deploy: the model, the version, the commit SHA, the repo it came from (if any) and its `glyphh.json` manifest |
| `event` | A deploy the server asked for that could not be done: the model, version, commit and why |
| `usage` | Once a minute at most: for each runtime key used, its SHA-256, a count of calls and when it was last used; and the total number of precedents across the Ada models |
| `ack`, `pong` | Acknowledgements and keep-alives |

The server calls no model over the link: a run's inputs and results stay on your rrotor. When a caller signs in to Glyphh rather than using a runtime key, rrotor asks the server whether
that sign-in is good for it (`POST /v1/runtime/introspect`, with the token the caller presented) and keeps the
answer a minute. With `RROTOR_LINK=off` none of this happens, and callers use runtime keys.

## Grace, and past it

A key is current until `expires_at` (the end of the paid month). A new one comes with each payment, over the
heartbeat or the link, so a running subscription never lapses.

- **Past `expires_at`, for 14 days:** everything runs on the key's limits. The health says `grace` and how many
  days are left, and rrotor logs a warning daily.
- **More than 14 days past:** Community's limits: the precedents past 5,000 stay and are served, and a record
  that would add one answers `limit_exceeded` (`indexed: false`, a finished call, not a failure). Nothing crashes, nothing is deleted, no model is refused,
  and a new key from any source lifts the limits at once.

## Air-gapped

1. Take the key from the platform.
2. Give it to rrotor in `GLYPHH_LICENSE_KEY`, or in the file `/etc/rrotor/license.key` (or `/data/licence.token`).
   `PUT /v1/licence` is refused on a self-hosted rrotor (403 `E_LOCAL_ONLY`): anyone with a runtime key could
   otherwise hand it a key.
3. Set `RROTOR_LINK=off`. rrotor makes no network request for its key, or for anything else of Glyphh's.
4. Each month, put the next key in the file and send rrotor `SIGHUP` (`docker compose kill -s HUP rrotor`), or wait
   up to an hour: rrotor reads the file again and holds the newer key. Nothing restarts.

A key cannot be called back once issued: it holds until its `expires_at` plus 14 days.
