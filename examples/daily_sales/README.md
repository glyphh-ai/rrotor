# daily_sales

The sample model of rrotor's `docs/architecture.md` (section 2), base edition: it runs on the base image alone.

- `start {day?}` reads `sales/<day>.csv` from its folder (yesterday by default), adds it up, converts the total to
  euros with `api.frankfurter.dev` (its one declared host), asks approval for its `write` step and writes the day to
  `ledger.json`. Declined, it answers the totals and writes nothing.
- `summary` answers the ledger.

`files/` seeds its folder on the first deploy. Its tests run without a sandbox, with `rrotor.testing`:

```sh
uv run pytest examples/daily_sales/tests --no-cov -p no:cacheprovider
```
