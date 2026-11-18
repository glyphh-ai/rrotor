# hubspot_pipeline

A pipeline into the rrotor's graph (`docs/architecture.md`, section 5.9): HubSpot's companies, contacts and deals,
written as nodes with the edges between them, each hour.

- `sync` runs three steps, each a plain function: **extract** reads a kind of record from `api.hubapi.com` a page at a
  time, **transform** flattens each record into a row, **store** writes the rows with `ctx.graph.rows`. A company is a
  `company` node, a contact a `contact` that `works_at` its company, a deal a `deal` that `belongs_to` its company
  and `involves` its contacts.
- Its manifest says `"graph": "write"`, declares the secret `HUBSPOT_TOKEN` (a HubSpot private app's token with the
  CRM read scopes) and the one host it reaches. Set the secret in Claude with the secrets form (`secrets_form`), or
  with `PUT /v1/programs/hubspot_pipeline/secrets/HUBSPOT_TOKEN`: never in the repo, never in the chat.
- Its schedule `each_hour` has the runtime call `sync` at five past every hour. Run again, it writes the same nodes
  again: the graph keeps one of each `(type, key)`.

Then anything on the rrotor reads the graph: `graph_search` and `graph_schema` from Claude, or an app's functions
with `"graph": "read"` and `ctx.graph.search(...)`.

Its tests run without a sandbox, HubSpot answered by a fake and the graph kept in memory (`rrotor.testing.FakeGraph`):

```sh
uv run pytest examples/hubspot_pipeline/tests --no-cov -p no:cacheprovider --import-mode=importlib
```
