# rrotor

The isolated runtime Ada installs into. It runs models: deterministic Python jobs (`load`, `start`, custom functions, `end`) that Claude and Claude's routines call; an Ada model is one. One locked-down, headless Linux image, run locally on every plan and hosted (one per person, on Fly) on Pro and Team.

The design is in `glyphh-ai/server`, `docs/architecture.md` ("`ada`: models and rrotor", and "rrotor"); how the parts call each other is in this repo's `docs/architecture.md`.

## Ada

An Ada model is a program rrotor runs: `rrotor.ada` opens its spec and precedent store on the Ada part; a changed encoding re-encodes the store on deploy or refuses it (`docs/architecture.md`, section 9). `examples/ada_tickets/` is one, with its tests.

## Parts

What a mod installs into rrotor beyond the base, signed with Glyphh's release key and licensed by the plan (`docs/architecture.md`, section 10). `scripts/part.py` builds and signs one; `parts/ada/` is the Ada part's recipe, built in CI.

## Benchmarks

`benchmarks/credit/`: an Ada model against an LLM and a classic model on real loan decisions, the same hold-out for all, with cost and time (2026-10-16; Ada first on the bank's cost since 2026-10-17). `benchmarks/banking77/`: routing a customer's message to one of 77 intents from its text alone, where the LLM and a classic text model are ahead of Ada today, with what would move it. `benchmarks/issues/`: free-text issue triage, kept as the counter-example.
