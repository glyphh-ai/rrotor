# Reference Rotors

Canonical, runnable [RotorSpec](../SPEC.md) documents. Each is a complete Rotor
Document (`apiVersion` / `kind` / `metadata` / `spec`), validates against
[`spec/schema/rotor.schema.json`](../spec/schema/rotor.schema.json) +
[`step.schema.json`](../spec/schema/step.schema.json), and is executable by the
rrotor runtime.

Validate any of them:

```bash
rrotor validate rotors/base.rotor.yaml
```

## The rotors

| Rotor | File | Shape (SPEC §15) |
| --- | --- | --- |
| **base** | [`base.rotor.yaml`](./base.rotor.yaml) | `ask → plan → execute → test` — the default (§15.1) |
| **base-single** | [`base-single.rotor.yaml`](./base-single.rotor.yaml) | `compose → respond → deliver` — the single-model chat default |
| **base-memory** | [`base-memory.rotor.yaml`](./base-memory.rotor.yaml) | `remember → facts → recall → compose → respond → deliver` — base-single + durable memory |
| **code** | [`code.rotor.yaml`](./code.rotor.yaml) | `plan (frontier) → plan.md → sub-rotor build → review (frontier) → gate` — the coding CONDUCTOR: plan-as-artifact, delegated build, frontier review |
| **build** | [`build.rotor.yaml`](./build.rotor.yaml) | `read-plan → code (local) → apply → setup → verify → gate` — the build WORKER the conductor delegates to |
| **router** | [`router.rotor.yaml`](./router.rotor.yaml) | `triage → route (router) → parse → branch → sub-rotor(chat\|code)` — the front door: one chat surface, many rotors |
| **web-dev** | [`web-dev.rotor.yaml`](./web-dev.rotor.yaml) | `recall → plan → scaffold → code → test → escalate` (§15.2) |
| **corp-data-slim** | [`corp-data-slim.rotor.yaml`](./corp-data-slim.rotor.yaml) | `retrieve → ground → slim model → prompt` (§15.2) |

## What each demonstrates

| Feature | base | web-dev | corp-data-slim |
| --- | :---: | :---: | :---: |
| `prompt` bounded compose (§7.1) | ✓ (ask) | | ✓ (final answer) |
| `plan` typed constrained decode (§7.10) | ✓ | ✓ | |
| `tool` deterministic effect (§7.16) | ✓ (mcp) | ✓ (app) | |
| `model` local lane (§7.2) | | ✓ (coder) | ✓ (slim, hard-grounded) |
| `gate` accept/reject/escalate (§7.8) | ✓ | ✓ | |
| Gate refine loop-back — `on_fail → plan` (§7.8) | ✓ | | |
| Escalation ladder → frontier → human (§7.15 / §9) | ✓ | ✓ | |
| `assert` grounded terminal / refuse (§7.9) | ✓ | ✓ | |
| `retrieve.sql` closed-op query (§7.5) | | | ✓ |
| `retrieve.kb` associative recall (§7.6) | | | ✓ |
| `retrieve.vector` semantic recall (§7.7) | | ✓ | |
| `hdc.map` grounding bridge (§7.3) | | | ✓ |
| Step-level cache on retrieval (§8) | | ✓ | |
| Prompt-prefix caching breakpoints (§8) | ✓ | ✓ | ✓ |
| Deny-by-default access grants (§13.4) | | | ✓ |
| Declared attention budget (§10.4) | ✓ (revolutions) | ✓ (wall_ms/tokens) | ✓ (tokens/cost) |
| Declared HDC space (§15.4) | ✓ | ✓ | ✓ |

### base.rotor.yaml

The rotor RotorSpec ships as `glyphh/base@0.1.0` — the fallback for any task with
no domain rotor. `ask` composes a grounded prompt, `plan` picks a typed op,
`execute` runs a deterministic tool, and the `test` gate verifies. On reject the
gate **loops back to `plan`** to refine; the attention `revolutions` budget bounds
the cycle; a repeated / low-margin failure **escalates** to a governed frontier
lane (falling back to a human rung). On pass the answer is **asserted**.

### base-single.rotor.yaml

THE single-model chat loop and `rrotor chat`'s default: `compose` builds the
bounded prompt (stable system block + the user turn), one bound model role
(`assistant`) answers, and the terminal `assert` delivers or refuses. No
planner/coder/evaluator split — the frictionless per-turn default.

### base-memory.rotor.yaml

`base-single` plus durable memory: `remember` **absorbs** the turn into the
stator (§7.4 — "my name is Tim" becomes a structured `(user, name, Tim)` fact,
"always …" becomes a standing directive), then recall runs both ways — the
user's full fact node (`retrieve.kb`, §7.6) and semantically similar earlier
turns (`retrieve.vector`, §7.7) — and `compose` carries both into the prompt.
Chat with it: `rrotor chat rotors/base-memory.rotor.yaml`. Memory persists
across restarts when the stator is durable (`ROTOR_STATOR_BACKEND=sqlite`);
otherwise it lives for the process.

### code.rotor.yaml

The conversation-native coding loop — ONE input (the chat turn), so `rrotor
chat code` drives it like any other rotor. The **planner** model derives the
structure a parameterized job would have declared as inputs: it emits a strict
`FILE:` / `TEST:` header plus the plan, and the deterministic `extract` step
(transform §7.17 `parse`) lifts the fields out — pure regex, replay-safe; a
malformed plan **refuses** (`E_BAD_PLAN`) rather than guessing. The **coder**
model then writes the complete file, the workbench applies it (`file.write`)
and runs the planner's proof (`shell.bash`). Verification failure loops back to
the **planner** with the full evidence — prior plan, prior code, and the
failing output — so every revolution can revise the plan *including the test
itself* (a wrong test can never pass), bounded by a 3-revolution attention
budget; exhaustion escalates (frontier → human) and an unverified change
refuses rather than claiming success. Two separately-bindable roles
(`planner`, `coder`), `mode: code` (fs + shell + git), `display` labels on
every step.

```bash
rrotor chat code
you › create a script named greet.js that prints "Hello, Chris!"
```

### router.rotor.yaml

The front door — pin THIS rotor in chat (`rrotor chat router`) and every
turn is classified by a **router** model (a third bindable role; pin it to a
tiny fast model) into a closed two-way decision, deterministically parsed
(bare word alone on a line — a format that cannot false-match an instruction
echo), and dispatched as a **sub-rotor call** (§7.19): conversation →
`glyphh/base-memory`, a build request → `glyphh/code`. Unparseable →
conversation, the lane that cannot mutate anything. The callee joins the
caller's session, so memory carries across lanes — teach it your name in a
chat turn, and the coding turns know it too. Note `mode: code`: a sub-rotor
shares the caller's run plugins, so the parent carries the union of its
children's tool surfaces.

### web-dev.rotor.yaml

A domain rotor that composes the base grounding cycle. It opens with a **cached
`retrieve.vector`** over framework docs / prior snippets (repeat builds over the
same corpus skip re-embedding), plans the build, scaffolds via a `tool` step, has
a **local coder model** write the code, and gates it. A gate failure **escalates**
to a governed frontier coder rather than looping forever.

### corp-data-slim.rotor.yaml

The "corp-data → slim model with a prompt call last" pattern. Deterministic
`retrieve.sql` + `retrieve.kb` pull exact facts, `hdc.map` grounds them, a **slim
local model** reasons over only the grounded facts (hard-grounded so an
unsupported claim refuses), and a **final `prompt` step** composes the answer. It
declares **deny-by-default access grants** (named CRM fields + the `corp` space's
relational roles only — the `finance` space is never granted) and an **attention
budget**.

## Determinism & termination

Every rotor here satisfies the static graph checks the validator enforces beyond
the schema: unique step ids, every edge target resolves, every reachable step can
reach a terminal (`end` / `__fail__` / a refusing `assert` / a `fail`), every loop
carries a cap, and `spec.space` is declared wherever a retrieval / gate / encode
step binds it.
