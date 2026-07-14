# The glyphh Client SDK — Implementation Specification

**Status:** v0.1 draft · **Package:** `@glyphh/client` · **Language:** TypeScript ·
**License:** proprietary (not open source) · **Targets:** the OpenRotor runtime
streaming transport `rotor.stream/v1`.

This is the build spec for the **client SDK**: the library every glyphh client
(desktop, mobile, CLI) consumes to talk to a rotor server. It is written to be
implemented directly — the wire protocol it targets is already shipped in this
runtime (`src/transport/`), so every endpoint and message below is real.

> **Two SDKs, do not confuse them.** This is the *client* SDK — for **consuming** a
> rotor server over the wire. It is distinct from the runtime's *plugin/tool* SDK
> (`ToolSpec`, the plugin interfaces) which **extends** the engine server-side.
> Different audience (app builders vs. runtime extenders), different package.

---

## 1. Scope

### 1.1 What the SDK is

The single client-side implementation of the rotor-server protocol plus the
session mechanics every client needs: auth, connection lifecycle, streaming,
**durable reconnect**, attachments, workspace access, and typed event decoding.
It exposes a small typed API; a client is a thin renderer on top of it.

```
┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│  desktop    │   │   mobile    │   │     CLI     │     presentation only
└──────┬──────┘   └──────┬──────┘   └──────┬──────┘
       └─────────────────┼─────────────────┘
                  ┌───────┴────────┐
                  │  @glyphh/client │                  session mechanics (this spec)
                  └───────┬────────┘
                          │  rotor.stream/v1 (SSE / WebSocket + HTTP control)
                  ┌───────┴────────┐
                  │  rotor server  │                   the runtime (openrotor serve)
                  └────────────────┘
```

### 1.2 What the SDK is not

- Not the runtime. It never executes a rotor; it drives one on a server.
- Not the plugin/tool SDK.
- Not a UI. No rendering, no terminal, no DOM.

### 1.3 Design goals

1. **Local and cloud are one call.** The only difference between a loopback server
   and a fly.io session is the base URL + credential.
2. **Durable by default.** A dropped connection resumes from a cursor, never loses
   or duplicates a turn — the runtime is deterministic and checkpointed; the SDK
   exposes that as automatic reconnect.
3. **Transport-agnostic surface.** SSE vs WebSocket is an internal detail; the
   public API is identical over either.
4. **Typed end to end.** Events are a discriminated union; errors map to the
   runtime's taxonomy.

---

## 2. The wire protocol it targets (`rotor.stream/v1`)

Already implemented in the runtime (`src/transport/`). The SDK is a client of exactly this.

### 2.1 Event model

Every run streams the same ordered sequence, each frame carrying a monotonic
`seq` **cursor** (the resume key):

```
open  →  step*  →  (answer | interrupt | error)  →  done
```

```ts
type WireEvent =
  | { seq: number; kind: "open";      wire: string; run_id: string; trace_id: string; rotor: string; session?: string }
  | { seq: number; kind: "step";      step_id: string; type: string; status: string; frames: string[]; tick: number; error?: string }
  | { seq: number; kind: "answer";    text: string }
  | { seq: number; kind: "interrupt"; step_id: string; awaiting: unknown }
  | { seq: number; kind: "error";     code: string; detail: string; remediation: string }
  | { seq: number; kind: "done";      status: string; terminal: string; outputs: unknown };
```

The sequence is **reconstructible from the persisted tape**: identical `seq`
whether streamed live or replayed after a reconnect. `run_id` is in the first
frame (`open`) so a client that drops immediately can still reconnect.

### 2.2 HTTP + SSE endpoints

| Method / path | Purpose | Notes |
|---|---|---|
| `POST /run` | run a rotor | buffered JSON by default; **SSE stream** when `Accept: text/event-stream`. Body: `{ rotor, inputs?, session? }`. `rotor` is inline YAML/JSON or a parsed document. |
| `GET /runs/:id/events?from=<seq>` | **durable reconnect** | SSE replay of the run after the cursor. Also honors the `Last-Event-ID` header (what a browser `EventSource` sends). `from=-1` (default) replays from `open`. 404 if the run is unknown. |
| `POST /runs/:id/resume` | HITL approval | body `{ payload?, decision?, timeout? }`. Continues an interrupted run. |
| `GET /healthz` · `/readyz` · `/version` | probes | `readyz` reports per-seam readiness incl. `local→live/stub`. |

SSE framing: `id: <seq>`, `event: <kind>`, `data: <json WireEvent>`. A leading
`: rotor.stream/v1` comment announces the protocol version.

### 2.3 WebSocket endpoint (`GET /ws`)

Bidirectional over one socket. On connect the server sends `{"type":"ready","wire":"rotor.stream/v1"}`.
Client → server control messages (JSON text frames):

```jsonc
{ "type": "turn",   "rotor": <yaml|object>, "inputs": {…}, "session": "…" }
{ "type": "resume", "run_id": "…", "payload": {…} }   // HITL approval
{ "type": "attach", "run_id": "…", "from": <seq> }    // durable reconnect
{ "type": "ping" }                                     // app-level keepalive → {"type":"pong"}
```

Server → client: the `WireEvent` frames for the active run, plus `{"type":"error",…}`
control frames for protocol errors (bad JSON, invalid rotor, unknown run/type).
The server processes control messages **in order** — one run streams fully before
the next is handled, so turns never interleave on a socket. Standard WS
ping/pong/close control frames are handled; binary frames are rejected.

---

## 3. Public API

The surface a client depends on. Names are normative; shapes may be refined
during implementation but the semantics are fixed.

### 3.1 Client + session

```ts
import { createClient } from "@glyphh/client";

const client = createClient({
  url: "http://127.0.0.1:8080",     // loopback local OR https://<session>.fly.dev
  token?: string | () => Promise<string>,   // omitted for no-auth loopback
  transport?: "auto" | "sse" | "ws",         // default "auto" (§5)
  fetch?: typeof fetch,                       // injectable for RN / tests
});

const session = client.session({
  rotor: "base" | RotorDocument | string,     // name resolved server-side, or inline
  model?: string,                             // fallback model, default "auto"
  sessionId?: string,                         // memory-tier scope; generated if absent
});
```

### 3.2 Running a turn

`turn()` returns an **async iterable** of typed events *and* a promise-like handle
for the terminal result. Both must work:

```ts
const run = session.turn(prompt, { inputs?, signal? });

for await (const ev of run.events) {
  // ev: TurnEvent — open | step | answer | interrupt | error | done
}
const result = await run.done;   // { runId, status, terminal, outputs, error? }
```

- `run.runId` resolves as soon as the `open` frame arrives (needed for reconnect).
- The SDK **owns reconnect**: if the connection drops mid-run, it re-attaches at
  `GET /runs/:id/events?from=<lastSeq>` (or WS `attach`), dedupes by `seq`, and the
  consumer's `for await` continues seamlessly. This is the headline feature.

### 3.3 Human-in-the-loop

```ts
for await (const ev of run.events) {
  if (ev.kind === "interrupt") {
    const decision = await ui.ask(ev);         // client renders the approval
    await session.resume(run.runId, { decision });
  }
}
```

### 3.4 Reattach explicitly

```ts
const run = session.attach(runId, { from?: number });   // resume watching a run
```

### 3.5 Cancel

```ts
run.cancel();   // detach the client from the stream
```

> **v1 semantics:** `cancel()` stops the client consuming and closes the stream.
> True server-side abort (halting an in-flight run) requires an executor abort
> signal that does not exist yet — see §10. Document `cancel()` as detach-only in v1.

---

## 4. Session durability (the core requirement)

The SDK must make a dropped connection invisible to the consumer.

1. Track the highest `seq` delivered for the active run.
2. On transport error while a run is live, reconnect with backoff (exponential,
   jittered; cap ~30s) to `GET /runs/:id/events?from=<lastSeq>` (SSE) or send WS
   `attach { run_id, from: lastSeq }`.
3. **Dedupe by `seq`** — the replay is at-least-once; drop any `seq <= lastSeq`.
4. Continue delivering to the same `for await`. Emit an SDK-level `reconnecting`
   / `reconnected` lifecycle event so a client can show a subtle indicator.
5. If the run already terminated during the outage, the replay ends at `done`; the
   consumer sees the tail exactly once.

This works because the runtime persists each `StepRecord` to the tape **before**
streaming it (`executor.ts`), so every `seq` the client saw is durable.

---

## 5. Transport selection

- `"auto"` (default): prefer **WebSocket** when the environment supports it and the
  URL scheme permits; fall back to **SSE + POST** on failure or restricted networks.
- `"sse"`: server→client over `EventSource`-style SSE; client→server (`resume`,
  new `turn`) over `POST`. Zero special infra; best compatibility.
- `"ws"`: single duplex socket.

The public API in §3 is identical across all three. A `Transport` interface
internal to the SDK abstracts: `openTurn`, `attach`, `resume`, `close`. Implement
`SseTransport` and `WsTransport` against it.

---

## 6. Auth

- **Local loopback:** typically no token. `createClient({ url })` works unadorned.
- **Cloud:** `token` is a bearer credential (or an async supplier for refresh). The
  SDK attaches `Authorization: Bearer <token>` to HTTP requests and the WS upgrade,
  and calls the supplier again on `401` before one retry.
- The SDK never persists credentials; the host app owns storage.

(The runtime does not yet enforce auth on these endpoints — §10. The SDK ships the
credential plumbing now so clients don't change when the server adds enforcement.)

---

## 7. Attachments & workspace

These require **new runtime endpoints** (§10); spec the SDK surface now so it's stable.

```ts
await session.attach_file(fileOrBlob, { path?: string });   // upload into the run workspace
const tree  = await client.workspace(sessionId).list(path?);
const bytes = await client.workspace(sessionId).read(path);
```

Map to (proposed) `POST /sessions/:id/files`, `GET /sessions/:id/files?path=`,
`GET /sessions/:id/files/:path`. Until the runtime exposes them, these throw
`E_UNSUPPORTED`; the API shape is frozen here so clients compile against it.

---

## 8. Errors

Two layers, both typed:

- **Run errors** arrive as an `error` `WireEvent` (`code`, `detail`, `remediation`)
  — the code is a runtime taxonomy code (see `docs/errors.md`, `openrotor errors`).
  Surface as `RunError` carrying those fields; do not throw for these (they are run
  outcomes), deliver them in the event stream and reflect in `run.done`.
- **Transport/SDK errors** (connect failure, auth, protocol/version mismatch,
  malformed frame) throw typed `SdkError` subclasses: `ConnectError`, `AuthError`,
  `ProtocolError`, `UnsupportedError`. Reconnectable transport drops are handled
  internally (§4), not thrown, unless retries are exhausted.

The SDK re-exports the taxonomy code list so clients can branch on codes without a
runtime dependency.

---

## 9. Versioning

- On connect, read `wire` from the `open`/`ready` frame. If its major version does
  not match the SDK's supported protocol, throw `ProtocolError` — do not guess.
- SDK is semver; the protocol version (`rotor.stream/vN`) is independent and
  negotiated at runtime.
- Package: `@glyphh/client`, dual ESM/CJS, `.d.ts` shipped, published to the private
  registry. Proprietary license header on every file. No telemetry by default.

---

## 10. Runtime gaps the SDK depends on (hand-off list)

The streaming/turn/resume/attach core is **done**. These are needed to complete the
SDK surface and should be built in the runtime alongside the SDK:

1. **Auth enforcement** on the stream/run/resume endpoints + the `/ws` upgrade
   (bearer; loopback exempt). SDK plumbing exists (§6); server enforcement pending.
2. **Attachments + workspace endpoints** (§7): upload/list/read against the session
   workspace sandbox (`ROTOR_WORKSPACE`).
3. **Session-level stream** (optional, post-v1): a conversation groups turns; a
   `GET /sessions/:id/stream` that spans runs would let a client reconnect to a
   whole session, not just a run. v1 reconnect is per-run, which covers the common
   mid-turn drop.
4. **Server-side cancel** (§3.5): an executor abort signal so `cancel()` halts an
   in-flight run rather than only detaching.

---

## 11. Test requirements (for the implementing agent)

Test against a **real `openrotor serve`** instance (boot it on an ephemeral port),
not a mock — the transport is cheap to run:

1. Turn streaming over both SSE and WS yields `open → step* → answer → done` in
   order with monotonic `seq`.
2. **Durable reconnect:** kill the transport mid-run, assert the consumer's async
   iterable still completes with no gap and no duplicate `seq`.
3. HITL: an interrupting rotor surfaces `interrupt`; `resume` drives it to `done`.
4. Attach: reconnect to a completed run from a cursor reproduces the tail once.
5. Auth: a `401` triggers exactly one token-refresh + retry.
6. Version mismatch throws `ProtocolError`.
7. Transport parity: the same test suite passes with `transport: "sse"` and
   `transport: "ws"`.

---

## 12. Build order

1. Types + protocol constants (`WireEvent`, `rotor.stream/v1`, error codes).
2. `Transport` interface + `SseTransport` (covers everything; simplest infra).
3. `createClient` / `session` / `turn` over SSE, with the terminal-result promise.
4. **Durable reconnect** (§4) — the feature that justifies the SDK.
5. HITL `resume` + `attach`.
6. `WsTransport`; run the whole suite under both transports (§11.7).
7. Auth plumbing (§6); attachments/workspace stubs throwing `E_UNSUPPORTED` (§7).
8. Package + publish to the private registry.

**Tell:** when the runtime's own dev CLI can be re-implemented as a thin `@glyphh/client`
consumer with a terminal renderer, the SDK is proven — and desktop/mobile are the
same work with a different skin.
