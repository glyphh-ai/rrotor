# Licensing a self-hosted rrotor

Written for the people who review what a piece of software sends out of their network. In short: rrotor checks a
signed licence on the machine, with no network request; it renews that licence over the link or a small daily
HTTPS call whose whole body is shown below; and an air-gapped rrotor never calls out at all.

## The licence

A licence is a JSON object `{payload, signature}`. The signature is Ed25519 over the payload's canonical JSON (keys
sorted, no spaces, ASCII only), made by the Glyphh server with its licence key. rrotor checks it with the public
half, which is baked into the image (`RROTOR_LICENCE_PUBLIC_KEY`): checking needs no network, and a licence changed
by one byte is refused.

| Field | What it says |
|---|---|
| `user_id`, `org_id` | The account and organisation that registered the runtime |
| `install_id` | Always `null` for a runtime's licence |
| `plan`, `limits` | The plan and its limits for runs |
| `mods` | Each mod the organisation has, with its limits: Ada's being here is what lets Ada models run |
| `expires_at` | When it stops being current (UTC) |
| `runtime_id` | The one runtime it is for |
| `licence_id` | This licence's own id (a UUID) |
| `kind` | How it came: `link`, `heartbeat` or `offline` |
| `grace_days` | How many days it still holds past `expires_at` (absent means none) |

Nothing else is in it: no names, no email addresses, no machine details. You can read any licence you hold; an
offline token is base64url of that JSON:

```sh
printf %s "$RROTOR_LICENCE" | tr '_-' '/+' | base64 -d | python3 -m json.tool
```

`GET /v1/licence` shows the licence rrotor holds, and `GET /v1/health` its state, `grace` with the days left once
it is past `expires_at`.

## Three ways it arrives

| Path | How | Lasts | Network |
|---|---|---|---|
| **Link** | The server's `welcome`, each time rrotor's link connects | 24 hours, then 14 days' grace | The link: an outbound WebSocket to `api.glyphh.ai:443` |
| **Heartbeat** | rrotor asks for one at boot and every 24 hours | 24 hours, then 14 days' grace | One HTTPS `POST` to `api.glyphh.ai:443` |
| **Offline** | You issue it on the platform and hand it to rrotor | Up to 365 days, then 14 days' grace | None |

With `RROTOR_LINK=on` (the default) rrotor uses both the link and the heartbeat; either alone keeps the licence
current, so a proxy that refuses WebSockets but passes ordinary HTTPS still leaves rrotor licensed. With
`RROTOR_LINK=off` it makes neither call and runs on an offline licence alone.

rrotor keeps whichever valid licence expires last, so an offline licence and a daily one can be held together: the
daily ones never shorten it.

## The heartbeat, exactly

At boot and then every 24 hours:

```http
POST /v1/license/verify HTTP/1.1
Host: api.glyphh.ai
Authorization: Bearer <runtime token>
Content-Type: application/json

{"machine": "<64 hex characters>", "licence_id": "<uuid or null>", "version": "<rrotor version>", "active_runs": <int>}
```

| Field | What it is | Why |
|---|---|---|
| `machine` | SHA-256 (hex) of `<runtime id>:<machine id>`, where the machine id is the host's `/etc/machine-id` when the container can read one, else a random id rrotor makes once and keeps in `/data` | To tell apart two machines running the same runtime. It is a hash: the machine id cannot be read back from it |
| `licence_id` | The id of the licence rrotor holds now, or `null` | To see which licence is in use |
| `version` | rrotor's version, e.g. `0.4.0` | To show it on the platform |
| `active_runs` | How many runs are in progress, a number | A count only, nothing about the runs |

The answer is `200 {"licence": {"payload": …, "signature": …}}`: a licence of kind `heartbeat`, current for 24
hours, with `grace_days: 14`. If the call fails or is refused, rrotor keeps the licence it has and tries again.

**Never sent, in the heartbeat or anywhere it is stored:** IP addresses, hostnames, prompts, model inputs or
outputs, model code, files, secrets, or anything about your users. The server keeps only four things from it:
`last_heartbeat_at`, `version`, `active_runs` and the `machine` hash. It does not store or log the caller's address
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
| `result` | The result of a run **the Glyphh server started** (a call through the Glyphh connector at `api.glyphh.ai/mcp`) |
| `event` | A model's own events, e.g. to start a routine |
| `usage` | Once a minute at most: for each runtime key used, its SHA-256, a count of calls and when it was last used |
| `ack`, `pong` | Acknowledgements and keep-alives |

Runs called at your rrotor's own address (`https://<public host>/mcp`) stay on it: their inputs and results never
go up the link. When a caller signs in to Glyphh rather than using a runtime key, rrotor asks the server whether
that sign-in is good for it (`POST /v1/runtime/introspect`, with the token the caller presented) and keeps the
answer a minute. With `RROTOR_LINK=off` none of this happens, and callers use runtime keys.

## Grace, and past it

A licence counts as live until `expires_at` plus `grace_days`. A rrotor that loses contact with Glyphh therefore
keeps everything running for 15 days on a daily licence (24 hours plus 14 days), and up to 379 on an offline one.

- **Within grace:** everything runs. `GET /v1/health` and `GET /v1/licence` say `grace` and how many days are left.
- **Past grace:** a run that needs a licensed mod, an Ada model, is refused with `E_NOT_IN_PLAN` ("the licence has
  expired"). Nothing crashes and nothing is deleted: rrotor keeps serving its API, models that need no licensed mod
  keep running, and it keeps trying to renew. A new licence, from any path, lifts the refusal at once.

## Air-gapped: the offline licence

1. On the platform, open the runtime and issue an offline licence for up to 365 days (the API:
   `POST /v1/runtimes/{id}/offline-licence {"days": 365}` answers `{token, expires_at}`).
2. Carry the token across, and give it to rrotor in one of three ways: the environment `RROTOR_LICENCE`, the file
   `/data/licence.token` (both read when rrotor starts). `PUT /v1/licence` is refused on a self-hosted rrotor
   (403 `E_LOCAL_ONLY`): anyone with a runtime key could otherwise hand it a licence, an older or smaller plan's
   that runs longer, and rrotor would keep it over the daily ones.
3. Set `RROTOR_LINK=off`. rrotor makes no network request for its licence, or for anything else of Glyphh's.
4. Before it expires, issue the next one, put it in the same place and restart rrotor; it keeps the one that
   expires last.

An offline licence cannot be called back once issued: it holds until its `expires_at` plus 14 days. Issue shorter
ones if that matters to you. Rotating the runtime's token on the platform stops the link and the heartbeat for the
old token at once.
