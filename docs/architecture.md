# rrotor architecture: the flows

How the parts of Glyphh work together, step by step: signing in, the runtimes, the Glyphh connector, and one sample program built, deployed, updated, run live and run on a schedule. The system design (what each part is, the data model, the plans) is in `glyphh-ai/server`, `docs/architecture.md`; this document follows it and shows the calls between the parts.

Each step is marked by its state: **built** (in `glyphh-ai/server` or the mod on branch `relaunch`, working), **designed** (decided in the server's architecture document, not built) or **proposed** (needed to make a flow work, not yet decided: listed under [Open](#open)).

## The parts

| Part | What it is | Where |
|---|---|---|
| **Person** | The user, in Claude | |
| **Claude** | Claude Code (desktop Code tab or CLI), and Claude's routines on Anthropic's machines | Their machine; Anthropic |
| **Mod** | The Glyphh mods: `glyphh` (account), `glyphh-workspaces`, `glyphh-rpa`, `glyphh-programs` | Inside Claude on their machine |
| **Pane** | Claude's own browser pane | Their machine |
| **Local rrotor** | The local runtime: browser and profiles, workspaces, RPA sites, programs | Their machine (Docker, OrbStack, Apple's `container`) |
| **Cloud** | The Glyphh server: API, device sign-in pages, Glyphh connector (`/mcp`), relay | Glyphh's cloud |
| **Hosted rrotor** | The hosted runtime, one per Pro or Team person: programs | Fly, provisioned by the Cloud |
| **Google / GitHub** | Sign-in providers | |
| **Stripe** | Checkout, subscriptions, portal | |

## 1. Signing in

### 1.1 The mod signs this machine in

Started by `/glyphh login`, or by asking Claude to sign in (Claude calls the mod's own tool `glyphh_login`). Both draw the sign-in inline in the chat: the code, then "Signed in as …".

![Sign in](img/seq-sign-in.png)

```mermaid
sequenceDiagram
    autonumber
    actor P as Person
    participant C as Claude
    participant M as Mod (glyphh)
    participant B as Pane
    participant G as Cloud
    participant I as Google / GitHub
    P->>M: /glyphh login (or asks Claude → glyphh_login tool)
    M->>G: POST /v1/device/code {name, surface, mod_version, machine_id}
    G-->>M: device_code, user_code, verification_uri_complete, interval
    M-->>C: inline sign-in in the reply row: the code
    Note over M,C: auto mode refuses the mod's own browser call, so the mod asks Claude (prompt.submit, just after the command)
    M->>C: "Open this in the browser pane: …/v1/device?code=XXXX-XXXX"
    C->>B: preview_start (reuses the tab already on the Glyphh site)
    P->>B: Continue with Google (or GitHub)
    B->>G: GET /auth/google/start?code=XXXX-XXXX (binds this browser with a cookie)
    G-->>B: redirect to Google
    P->>I: signs in, consents
    I-->>B: redirect to /auth/google/callback?code=…
    B->>G: callback
    G->>I: exchange the code, read the profile (GitHub: /user and /user/emails)
    Note over G: identity found by (provider, provider_user_id), first sign-in makes the user and a Free org (admin), or joins the org an invite is waiting in
    Note over G: install keyed by (user, machine_id): the same machine reuses its install, a new machine is refused past the plan's installs
    G-->>B: "Signed in. Go back to Claude."
    loop every interval (slow_down adds 5 s)
        M->>G: POST /v1/device/token {device_code}
        G-->>M: authorization_pending … then {access_token, install_id}
    end
    M->>M: ~/.glyphh/install.json (0600)
    M->>G: GET /v1/license, GET /v1/license/key
    G-->>M: license signed with Ed25519: plan, limits, mods, expiry
    M->>M: check the signature, keep ~/.glyphh/license.json
    M-->>C: the inline sign-in redraws: Signed in as … · Free
```

**State:** built, end to end, on 2026-10-14 (Google and GitHub, `/glyphh login`, `glyphh_login`, install reuse by machine).

- `/glyphh logout` revokes this machine's install (`POST /v1/sign-out`) and removes `install.json` and `license.json`.
- **Linking** a second provider to the same account (`POST /v1/me/identities` → the provider's page → the same callback, marked as a link): built.
- The license is refreshed at start and daily; a revoked install answers 401 and the mod signs out.

### 1.2 The local rrotor starts and links

The `glyphh` mod starts the local rrotor once the machine is signed in. The other mods work through it.

![Local rrotor](img/seq-local-rrotor.png)

```mermaid
sequenceDiagram
    autonumber
    participant M as Mod (glyphh)
    participant D as Docker / OrbStack / container
    participant R as Local rrotor
    participant G as Cloud
    M->>G: POST /v1/runtime/local {install_id} (proposed)
    G-->>M: runtime_id, runtime token (shown once)
    M->>D: run ghcr.io/glyphh-ai/rrotor:#lt;version#gt; with volume /data, port 127.0.0.1:#lt;port#gt;, the runtime token
    D->>R: boot: s6 starts the runner (databases start on demand)
    R->>G: WebSocket /v1/runtime/link, Authorization: Bearer #lt;runtime token#gt;
    R->>G: hello {protocol, rrotor version, mode: local}
    G-->>R: welcome {runtime_id}
    M->>R: local MCP on 127.0.0.1:#lt;port#gt; with a local token (proposed): the workspaces, RPA and programs tools
```

**State:** designed (the local rrotor, the outbound link); proposed: how the local runtime gets its token and how the mods reach it (see [Open](#open)).

### 1.3 The hosted rrotor, on upgrading to Pro or Team

![Hosted rrotor](img/seq-hosted-rrotor.png)

```mermaid
sequenceDiagram
    autonumber
    actor P as Person
    participant C as Claude
    participant M as Mod (glyphh)
    participant B as Pane
    participant S as Stripe
    participant G as Cloud
    participant F as Fly
    participant H as Hosted rrotor
    P->>M: Plan → Pro (Billing panel)
    M->>G: POST /v1/billing/checkout {plan: Pro, seats}
    G-->>M: Stripe Checkout address
    M->>C: "Open this in the browser pane: #lt;checkout address#gt;"
    C->>B: preview_start
    P->>S: pays in Checkout
    S->>G: webhook checkout.session.completed (signed)
    G->>G: org on Pro's newest plan version
    G->>F: create machine + volume (size = the person's storage cap), runtime token in its secrets (designed)
    F->>H: boot
    H->>G: WebSocket /v1/runtime/link, hello {mode: hosted}
    Note over G,H: idle → Cloud stops the machine, a call → Cloud starts it (Fly starts a stopped machine in seconds)
```

**State:** checkout and webhook built; provisioning designed.

### 1.4 The Glyphh connector: Claude and its routines allowed to call Glyphh

Routines run on Anthropic's machines, so they reach Glyphh through a connector added once at claude.ai/customize/connectors, signed in with OAuth.

![Connector](img/seq-connector.png)

```mermaid
sequenceDiagram
    autonumber
    actor P as Person
    participant A as Claude (claude.ai / Anthropic)
    participant G as Cloud
    participant I as Google / GitHub
    P->>A: Add custom connector: https://#lt;glyphh#gt;/mcp
    A->>G: OAuth dynamic client registration
    A->>G: GET /mcp/authorize (PKCE) → browser
    G-->>P: sign in (Google / GitHub) and "Allow Claude to use your Glyphh?" (proposed page)
    P->>I: signs in
    P->>G: Allow
    G-->>A: authorization code → POST /mcp/token → access token
    G->>G: connector_grants row {user, client, token_hash}
    A->>G: /mcp tools/list: programs_list, call, continue, run_status, live panel
```

**State:** designed (connector, OAuth endpoints, `connector_grants` table built); proposed: the Allow page.

- A grant is revoked one at a time; a revoked grant answers 401.
- The Cloud never holds a Claude login: Claude runs on the person's subscription and calls Glyphh's tools.

## 2. A program, end to end

**The sample:** `daily_sales`. Every morning it reads yesterday's sales from `analytics.example.com` (a site the person signs in to) and its API (an API key), adds them up, and hands back rows for a dashboard in Claude.

```python
from glyphh import Ctx, Result

def load(ctx: Ctx) -> None:
    ctx.page = ctx.browser.open("work", "https://analytics.example.com")

def start(ctx: Ctx, inputs: dict) -> Result:
    if ctx.page.signed_out():
        return Result.needs_signin(profile="work", site="analytics.example.com")
    day = inputs.get("day") or ctx.yesterday()
    rows = ctx.http.get("https://api.analytics.example.com/v1/sales",
                        params={"day": day}, key=ctx.secrets["ANALYTICS_KEY"]).json()
    total = sum(r["amount"] for r in rows)
    ctx.db.postgres.execute("insert into daily (day, total) values (%s, %s)", day, total)
    return Result.done({"day": day, "total": total, "rows": rows})

def end(ctx: Ctx) -> None:
    ctx.page.close()
```

Its manifest declares the function `start` (input `day`, optional), the hosts `analytics.example.com` and `api.analytics.example.com`, the secret `ANALYTICS_KEY`, the profile `work`, and Postgres.

### 2.1 Build, in Claude, against the local rrotor

![Build](img/seq-build.png)

```mermaid
sequenceDiagram
    autonumber
    actor P as Person
    participant C as Claude
    participant M as Mod (glyphh-programs)
    participant R as Local rrotor
    participant B as Pane / live panel
    P->>C: "Build a program that fills my sales dashboard every morning"
    C->>M: program_create daily_sales, file_write (code, manifest)
    M->>R: store the draft in /data/programs/daily_sales/draft
    C->>M: program_test daily_sales start
    M->>R: run: sandbox (bubblewrap + Landlock, egress only to declared hosts), load → start
    R-->>M: Result needs_signin {profile: work, site}
    M-->>C: needs_signin
    C->>B: open the live panel on the run (the profile's browser)
    P->>B: takes control, signs in to analytics.example.com (Claude sees nothing)
    B->>R: the sign-in stays in /data/profiles/work
    C->>M: program_test again
    R-->>M: Result failed {step: secrets, missing: ANALYTICS_KEY}
    C-->>P: "Add your analytics API key" (a field in the mod's panel, never in the chat)
    P->>M: types the key
    M->>R: secret stored, encrypted with the rrotor's key
    C->>M: program_test again
    R-->>M: Result done {day, total, rows}
    C-->>P: shows the result, fixes and re-tests until it is right
```

**State:** designed (programs mod and local rrotor not built).

### 2.2 Deploy: to the hosted rrotor, and the routine

![Deploy](img/seq-deploy.png)

```mermaid
sequenceDiagram
    autonumber
    participant C as Claude
    participant M as Mod (glyphh-programs)
    participant R as Local rrotor
    participant G as Cloud
    participant H as Hosted rrotor
    C->>M: program_deploy daily_sales
    M->>R: cut version 1: bundle + manifest, content hash
    M->>G: POST /v1/programs {name}, POST /v1/programs/{id}/versions {manifest, hash, bundle_ref}
    G->>H: wake if stopped, link: deploy {program, version 1, bundle}
    H->>H: /data/programs/daily_sales/1, its Postgres database and role
    H-->>G: deployed
    G->>G: deployments row (hosted rrotor, version 1, active)
    G-->>M: deployed
    Note over H: the hosted rrotor has its own browser profiles: its first run that needs the site answers needs_signin, signed in once through the live panel (secrets are set on it the same way)
    C->>C: create the routine (Claude's routines): "Every day at 7:07, run daily_sales and update my sales dashboard", Glyphh connector included
```

**State:** designed. (Local runs need no deploy: a local program runs where it was built.)

### 2.3 Update

![Update](img/seq-update.png)

```mermaid
sequenceDiagram
    autonumber
    participant C as Claude
    participant M as Mod (glyphh-programs)
    participant R as Local rrotor
    participant G as Cloud
    participant H as Hosted rrotor
    C->>M: file_write (the change), program_test
    M->>R: test in the sandbox until done
    C->>M: program_deploy daily_sales
    M->>G: POST /v1/programs/{id}/versions {version 2}
    G->>H: deploy {version 2}
    H-->>G: deployed
    G->>G: version 2 active, version 1 kept
    Note over G: a routine calls by name, so it gets version 2 next run, a rollback makes version 1 active again (POST /v1/programs/{id}/deploy {version 1})
```

**State:** designed.

### 2.4 Run live, from Claude

![Run live](img/seq-run-live.png)

```mermaid
sequenceDiagram
    autonumber
    actor P as Person
    participant C as Claude
    participant G as Cloud
    participant H as Hosted rrotor
    participant L as Live panel
    P->>C: "Run daily_sales for Monday"
    C->>G: connector call(daily_sales, start, {day: Monday})
    G->>G: check the grant, the plan's hours left
    G->>H: wake (Fly start) → link up → call {run_id, start, inputs}
    H->>H: sandbox: load → start
    H-->>L: frames and steps (relayed by the Cloud, WebRTC where it connects)
    C->>L: open the live panel (MCP App from the connector)
    H-->>G: Result needs_approval {step: "write Monday's total"}
    G-->>C: needs_approval
    C-->>P: "Write Monday's total?"
    P->>C: yes
    C->>G: continue(run_id, approved)
    G->>H: continue
    H-->>G: Result done {day, total, rows}
    G->>G: runs row: caller claude, status done, seconds (counted against the hours)
    G-->>C: done
    C-->>P: the numbers
```

**State:** designed.

### 2.5 Run on a schedule, back through Claude, output to a dashboard

The routine is the runner. Claude in the routine calls the program, answers what it asks, and writes the result to a dashboard (an artifact in Claude with its own database).

![Scheduled run](img/seq-scheduled.png)

```mermaid
sequenceDiagram
    autonumber
    participant A as Claude routine (Anthropic)
    participant G as Cloud
    participant H as Hosted rrotor
    participant D as Dashboard artifact (its db)
    actor P as Person
    Note over A: 7:07 every day, on the person's subscription
    A->>G: connector call(daily_sales, start)
    G->>H: wake → call {run_id}
    H-->>G: Result needs_input {choose: account, options: [EU, US]}
    G-->>A: needs_input
    A->>G: continue(run_id, {account: US}) (the routine's prompt says which)
    G->>H: continue
    H-->>G: Result done {day, total, rows}
    G-->>A: done
    A->>D: write the rows to the dashboard's database (ArtifactData batch)
    Note over A,D: or the dashboard reads them live itself (an artifact may call the Glyphh connector)
    alt a step needs approval, with nobody watching
        H-->>G: Result needs_approval
        G->>G: approvals row (waiting)
        G-->>P: push / email: "daily_sales needs you"
        G-->>A: needs_approval: the routine reports it and ends
        P->>G: approves later (the live panel or the mod)
        G->>H: continue → done
    end
```

**State:** designed. To prove: a scheduled routine writing to an artifact's database unattended, and an artifact calling a custom connector.

### 2.6 The Cloud starting a routine (calls back to Claude)

When something happens on Glyphh's side (a program finished, a page it watches changed, a waiting step was approved), the Cloud can start a routine through the routine's API trigger, with the data as its text.

![Routine fire](img/seq-routine-fire.png)

```mermaid
sequenceDiagram
    autonumber
    participant H as Hosted rrotor
    participant G as Cloud
    participant A as Claude routine (Anthropic)
    H-->>G: event {program, kind: changed, data}
    G->>A: POST /v1/claude_code/routines/#lt;id#gt;/fire {text: event JSON} (the person gave Glyphh this routine's trigger token)
    A->>A: reads the payload (its prompt says to act on it)
    A->>G: connector call(…) for the next step
```

**State:** designed. The routine's trigger token starts that routine only; it is not a Claude login.

## 3. What every call carries

| Between | How | Auth |
|---|---|---|
| Mod → Cloud | HTTPS, `/v1/...` | the install's token (Bearer) |
| Browser → Cloud sign-in pages | HTTPS | a short-lived cookie binding the browser to the sign-in |
| rrotor → Cloud | WebSocket `/v1/runtime/link`, outbound only | the runtime's token (Bearer header, never in a URL) |
| Cloud → rrotor | messages down the link: call, continue, cancel, deploy | the link itself |
| Claude / routines → Cloud | MCP over HTTPS, `/mcp` | the connector grant's token (OAuth) |
| Cloud → routine | HTTPS POST to the routine's `/fire` | the routine's trigger token |
| Mod → local rrotor | MCP on `127.0.0.1` | a local token (proposed) |
| Stripe → Cloud | webhook | Stripe's signature |

## Open

- **The local rrotor's token and reach** (1.2): `POST /v1/runtime/local` for its runtime token; the mods reach it as a local MCP server on `127.0.0.1` with a local token. Or the mods go through the Cloud over the same link as the hosted one.
- **The Allow page** for the connector's OAuth (1.4): one more server page, the only one besides the device page.
- **The live panel's transport:** frames over the link and the relay first; WebRTC end to end later.
- **Secrets on the hosted rrotor:** set again there (2.2), or carried from the local one encrypted to the hosted rrotor's key.
- **Per-program isolation in Neo4j:** the free edition holds one database.
