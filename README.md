# rrotor

The isolated runtime Ada installs into. It runs models: deterministic Python jobs (`load`, `start`, custom functions, `end`) that Claude and Claude's routines call; an Ada model is one. One locked-down, headless Linux image, run locally on every plan and hosted (one per person, on Fly) on Pro and Team.

The design is in `glyphh-ai/server`, `docs/architecture.md` ("`ada`: models and rrotor", and "rrotor"); how the parts call each other is in this repo's `docs/architecture.md`.

## Ada

An Ada model is a program rrotor runs: `rrotor.ada` opens its spec and precedent store on the Ada part; a changed encoding re-encodes the store on deploy or refuses it (`docs/architecture.md`, section 9). `examples/ada_tickets/` is one, with its tests.

## Parts

What a mod installs into rrotor beyond the base, signed with Glyphh's release key and licensed by the plan (`docs/architecture.md`, section 10). `scripts/part.py` builds and signs one; `parts/ada/` is the Ada part's recipe, built in CI.

## Benchmarks

| | Ada alone | Best LLM alone | Ada in front of the LLM |
|---|---|---|---|
| Loans: the bank's cost over 200 (lower is better) | **100** | 110 | |
| Support messages: 200 routed to 77 intents | 90.0% | 96.5% | **97.5%**, the LLM seeing 22%, at $0.22 against $300 |
| A session's memory: what a turn needs from earlier turns | recall by request 14%; recency 36%; a per-file ledger recovers it by construction | | history is 89% of every call's context: ~70% of the bill |

`benchmarks/credit/`: an Ada model against an LLM and a classic model on real loan decisions, the same hold-out for all, with cost and time (2026-10-16; Ada first on the bank's cost since 2026-10-17). `benchmarks/banking77/`: routing a customer's message to one of 77 intents from its text alone: Ada 88%, two points from a tuned classic text model, Fable and Opus at 96%, Sonnet 86%, Haiku 81%; Ada and Fable together 97.5% with the LLM seeing a fifth of the traffic, a thousand times cheaper than the LLM alone. `benchmarks/session/`: what a Claude Code turn needs from the turns before it, measured on 180 real sessions: history is 89% of every call's context and ~70% of the bill; a per-file ledger plus recency recovers the need, similarity over the request does not. `benchmarks/issues/`: free-text issue triage, kept as the counter-example.
