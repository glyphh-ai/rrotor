# rrotor

**Recursive Reasoning on top of reasoning.** rrotor lifts an AI agent's *loop* out
of the model and turns it into a declarative, deterministic, replayable artifact —
a *rotor*.

Version 0.1 · Draft · Apache-2.0 (Glyphh AI LLC) · The open reference runtime for
[RotorSpec](SPEC.md) · Production runtime: [glyphh-rotor]

## What "rrotor" means

The doubled **r** is the whole idea. A model *reasons*; **rrotor is Recursive
Reasoning — reasoning *about* that reasoning.** It wraps the model's stochastic step
inside a deterministic control loop that decides what runs next, grounds every claim
against memory, gates the result, and records the whole thing so it can be replayed
and audited. The model thinks; the rotor decides. (It's also, yes, a rotor that
rotates — hence the name.)

## Why we built it

An AI agent is a loop — and today that loop lives *inside* the model's context
window. It's re-derived from scratch on every prompt, tangled up with the tokens,
and impossible to replay, audit, or reason about. Change one line of a system prompt
and you've silently changed the entire machine. That's fine for a demo and miserable
for anything you have to operate, debug, or trust.

rrotor takes the opposite stance: **the loop is the artifact, not the prompt.** You
declare the steps an agent runs — `ask → plan → execute → test` — as a versioned
document, and rrotor executes them deterministically. The *control flow* is
replayable bit-for-bit; the model and tool outputs are checkpointed and never
re-invoked on replay (honest determinism — reproducible *decisions*, not reproducible
*tokens*). Memory is a first-class store (the **stator**), grounding is a gate where
"I don't know" is a valid terminal answer, and every turn is permanently on the
record.

The result is an agent you can **inspect, pin to a version, reproduce, and reason
about** — instead of a black box that happens to work today. rrotor is the open
Node/TypeScript runtime; the standard it implements is **RotorSpec**.

## What's a rotor?

A rotor is a versioned document of deterministic steps an agent runs for a class
of task. Most of the time it's the **base rotor** — `ask → plan → execute →
test`. But you can declare domain rotors: web development, slide generation,
prestige marketing, or "pull corp data into a slim on-device model with a prompt
call first and last." Composable, declarative, deterministic.

```yaml
apiVersion: rotorspec/v0.1
kind: Rotor
metadata: { name: base, version: 0.1.0 }
spec:
  steps:
    - { id: ask,     type: prompt }
    - { id: plan,    type: plan }
    - { id: execute, type: tool }
    - { id: test,    type: gate, on_reject: escalate }
```

## Getting started

Run the reference runtime locally. Node 20+.

```bash
npm install && npm run build && npm link   # once: the `rrotor` bin on your PATH

make model     # serve a local GGUF via llama.cpp (or set MODEL_URL to any OpenAI-compatible endpoint)
make tui       # the full-screen TUI · durable sqlite memory · live model
```

### The TUI is a chat

Just type. **Every line is one full rotor turn** — plan, memory recall, grounding,
and gating all run *behind the scenes*; only real tool actions surface as
collapsible receipts, and the answer streams to a `●` line. The loop's machinery
is on the record (every turn is checkpointed and replayable) without cluttering
the conversation.

| Key / command | What |
| --- | --- |
| `Esc` | **stop the running turn** — cuts an in-flight model call, keeps the session (Ctrl+C quits the app) |
| `shift+tab` | cycle rotors (`router` · `base-memory` · `code` · `base-single`) |
| `tab` · `ctrl+o` | fold a tool receipt · expand them all |
| `/model local <url>` | bind a model lane (see below) · `/model frontier <url> <key>` |
| `/store sqlite [path]` | durable memory · `/store memory` for ephemeral |
| `/theme <name>` | restyle (`dark` · `light` · `highvis` · `claude` · `aurora`) |
| `/rotor <name>` · `/quit` | switch rotor · leave |

Out of the box there's **no model bound**, so the loop still grounds, gates, and
remembers — it just can't *think*. Bind one with `/model local <url>` (or
`ROTOR_MODEL_URL`) and answers start streaming; until then the stub says so
rather than dumping its prompt.

`make help` lists every launcher — `chat` (readline harness), `serve` (HTTP
runtime), `repl`, `dev-tui` (run the TUI from source). Every mode variable
overrides per-invocation:

```bash
make tui ROTOR=code                        # pin the coding rotor
make tui STATOR=/tmp/scratch.db            # a throwaway memory store
make serve PORT=9000 MODEL_URL=http://…    # your endpoint
```

All settings can live in a `.env` (project dir or `~/.rrotor/.env`) — copy
[`.env.example`](./.env.example) for the full annotated catalog: model lanes,
provider keys (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`), the stator, serve,
logging, and the telemetry drain. Precedence: shell env > `./.env` >
`~/.rrotor/.env` > TUI prefs.

Or by hand — the runtime is configured entirely by environment:

```bash
ROTOR_STATOR_BACKEND=sqlite ROTOR_STATOR_URL=~/.rrotor/stator.db \
ROTOR_MODEL_URL=http://127.0.0.1:8080 rrotor
```

`rrotor serve` exposes the probe surface plus the streaming transport the
clients consume: `POST /run` (buffered or SSE), `GET /runs/:id/events` (durable
reconnect), and a WebSocket at `/ws`. The interactive product CLI (chat ·
co-work · code) is a **separate client** that talks to this server through the
glyphh client SDK — see [docs/sdk-spec.md](docs/sdk-spec.md).

Out of the box the model lane is a deterministic **stub** — the loop still
grounds, gates, and replays, it just doesn't call a real model. To make it
*think*, point it at any OpenAI-compatible endpoint:

```bash
# 1. serve a small local model (example: Qwen3-1.7B GGUF via llama.cpp)
pip install "llama-cpp-python[server]"
python3 -m llama_cpp.server --model Qwen3-1.7B-Q8_0.gguf \
  --model_alias glyphh-local --host 127.0.0.1 --port 8080

# 2. tell the runtime where it lives (local lane is free by construction)
export ROTOR_MODEL_URL=http://127.0.0.1:8080
npm run dev -- serve
```

`GET /readyz` reports `local→live` once the endpoint is reachable, `local→stub`
otherwise. Reasoning models (Qwen3, etc.) emit `<think>…</think>` first — append
`/no_think` to a prompt to skip it on slow CPUs.

## The pillars

- **Deterministic control plane / stochastic data plane** — which step runs next
  is replayable bit-for-bit; model and tool outputs are checkpointed, never
  re-invoked on replay. Honest determinism: RotorSpec never claims reproducible
  *tokens*, only reproducible *control flow*.
- **Grounding as a first-class gate** — a model proposal is admitted only when
  glyphh's HDC memory *returns* it; "I don't know" is a terminal, not an error.
- **Gateway** — the transport / mechanics / governance layer: I/O adapters,
  MCP ↔ API ↔ format translation, rate limiting / throttling, and metering /
  FinOps (cloud metered through the server, local free on local models).
- **Attention** — the dual of escalation: a *budget* bounding how long a rotor
  rotates (time / revolutions / tokens / cost) and *weights* stating which
  document signals it focuses on.
- **Escalation** — `local → frontier → human`, on typed triggers.

## Step catalog (closed vocabulary)

`prompt` · `model` · `hdc.map` · `retrieve.sql|kb|vector` · `write` · `gate` ·
`assert` · `plan` · `branch` · `loop` · `parallel` · `wait`/`interrupt` ·
`escalate` · `tool` · `transform` · `cascade` · `sub-rotor` · `fail`. Anthropic's
five agent patterns (prompt chaining, routing, parallelization, orchestrator-
worker, evaluator-optimizer) are *compositions* of these, not new step types.

## Reference implementations

Two, on purpose — independent implementations are how a standard proves it's
real:

- **This repo** ships a minimal, open **Node/TypeScript** reference executor
  _(in progress)_ — the conformance demo, readable in the language most
  developers reach for.
- **[glyphh-rotor]** is the production runtime (Python, closed): the full HDC
  substrate, evolved to conform. The patent-pending grounding method lives
  there, not here — this open repo abstracts `hdc.map`.

## Runtime & scaling

A rotor runtime is a self-contained instance of this spec: **stateless compute,
state in the stator (memory) backend, the gateway as the transport boundary.**
That makes it a clean Kubernetes citizen — a Deployment of interchangeable rotor
pods, scaled horizontally.

## Status & license

- **Open source — Apache-2.0** (see [LICENSE](LICENSE), [NOTICE](NOTICE)).
  Copyright © 2026 Glyphh AI LLC. Contributions are accepted under the same
  license — see [CONTRIBUTING.md](CONTRIBUTING.md).
- Draft **v0.1**: the spec text ([SPEC.md](SPEC.md) + [docs/](docs/)) is written;
  the JSON Schema and reference rotors are in progress.
- The patent-pending HDC grounding method is **not** disclosed or implemented
  here — this repository abstracts or stubs the `hdc.map` step. Apache-2.0's
  patent grant covers only the code in this repo, not the separate production
  grounding implementation (see [NOTICE](NOTICE)).
- Security reports: see [SECURITY.md](SECURITY.md).

## Layout

| Path | What |
| --- | --- |
| `SPEC.md` | the specification |
| `docs/` | concepts · execution model · glyphh integration |
| `spec/schema/` | JSON Schema _(in progress)_ |
| `rotors/` | reference rotors _(in progress)_ |
| `tools/` | validator _(in progress)_ |

[glyphh-rotor]: https://github.com/glyphh-ai/glyphh-rotor
