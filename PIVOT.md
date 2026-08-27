# Branch: memory-proxy-pivot

This repo is the **product core**: the memory-reconstruction proxy + stator.

The pivot:
- Keep the construction loop as the **hidden back-channel builder** — customers
  fill model *slots*, they do **not** author the loop *graph*; provenance is
  **readable, not editable**.
- Cut loops / harness as a customer-facing surface.
- Storage stays **model-independent**; the warmed prompt is constructed per-turn
  over the portable stator. Base tier is deterministic (no external model).

**Canonical writeup:** the `server` repo's `docs/README.md`.
**Recovery:** `git checkout main`, or the tag `pre-memory-proxy-pivot-2026-08-26`.
