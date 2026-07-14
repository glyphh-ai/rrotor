# RotorSpec

**The standard for deterministic AI agent loops.**

Version 0.1 · Draft · Proprietary (Glyphh AI LLC) · Reference executor: [glyphh-rotor]

An AI agent is a loop. Today that loop lives inside a model's context window, is
re-derived on every prompt, and can't be replayed, audited, or reasoned about.
**RotorSpec lifts the loop out of the model and makes it a declarative,
deterministic artifact** — a *rotor*.

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
npm install
npm run dev -- serve                                          # start the runtime (HTTP + streaming)
npm run dev -- run rotors/base.rotor.yaml prompt="Where does Ada live?"   # or run one rotor headless
```

`openrotor serve` exposes the probe surface plus the streaming transport the
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

- **Proprietary — © 2026 Glyphh AI LLC, all rights reserved** (see [LICENSE](LICENSE),
  [NOTICE](NOTICE)). This is a temporary, all-rights-reserved license for now; the
  project may be re-licensed under different (including open) terms in the future.
- Draft **v0.1**: the spec text ([SPEC.md](SPEC.md) + [docs/](docs/)) is written;
  the JSON Schema and reference rotors are in progress.
- The patent-pending HDC grounding method is not disclosed here — this repository
  abstracts or stubs the `hdc.map` step (see [NOTICE](NOTICE)).

## Layout

| Path | What |
| --- | --- |
| `SPEC.md` | the specification |
| `docs/` | concepts · execution model · glyphh integration |
| `spec/schema/` | JSON Schema _(in progress)_ |
| `rotors/` | reference rotors _(in progress)_ |
| `tools/` | validator _(in progress)_ |

[glyphh-rotor]: https://github.com/glyphh-ai/glyphh-rotor
