# daily_sales

A program that needs no part, used to test rrotor's base end to end (`docs/architecture.md`, section 2.2).

- `start {day?}` reads `sales/<day>.csv` from its folder (yesterday by default), adds it up, converts the total to
  euros with `api.frankfurter.dev` (its one declared host), asks approval for its `write` step and writes the day to
  `ledger.json`. Declined, it answers the totals and writes nothing.
- `summary` answers the ledger.
- Its schedule `each_morning` has the runtime call `start` (yesterday) at 06:15 UTC every day; the run waits for the
  `write` approval like any other (`GET /v1/approvals`), and expires after 15 minutes without one.

`files/` seeds its folder on the first deploy. Its tests run without a sandbox, with `rrotor.testing`:

```sh
uv run pytest examples/daily_sales/tests --no-cov -p no:cacheprovider
```
