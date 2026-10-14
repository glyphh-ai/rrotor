# rrotor

The isolated runtime that runs Glyphh programs: deterministic Python jobs (`load`, `start`, custom functions, `end`) that Claude and Claude's routines call. One locked-down, headless Linux image, run locally on every plan and hosted (one per person, on Fly) on Pro and Team.

The design is in `glyphh-ai/server`, `docs/architecture.md` ("glyphh-programs: programs and rrotor").
