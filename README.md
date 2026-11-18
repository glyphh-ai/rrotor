# rrotor

A local runtime for a person's programs: one locked-down, headless Linux image that deploys a program by commit, runs
each call in a sandbox, and answers over REST and MCP at its own address. Customers run it themselves, at `https://<host>/mcp`
([self-hosting](docs/self-hosting/README.md)); Glyphh-run hosting is off. It is added to Claude as a
connector (Glyphh sign-in or a runtime key) and called by apps with the `glyphh` SDKs (`glyphh-ai/glyphh`). Image:
`ghcr.io/glyphh-ai/rrotor`. Ada models do not run here: they run in Ada Cloud, on the Glyphh server.

Everything about a program stays on the runtime: its code, its runs, its secrets, kept encrypted with the runtime's
own key and read by the program as `ctx.secrets["NAME"]` ([architecture, 5.5](docs/architecture.md#55-secrets)), its
schedules, cron expressions in its manifest that the runtime fires itself
([5.6](docs/architecture.md#56-schedules)), and its SQLite database, a file in its data folder read as `ctx.db`
([5.3](docs/architecture.md#53-the-sdk-rrotor)). The Glyphh server only grants the licence.

A program can also be a **CLI** (`"kind": "cli"`, [5.7](docs/architecture.md#57-clis)): a command-line tool (the
glyphh CLI, `gh`) installed at deploy from a pinned, checksummed release tarball, npm or PyPI package, or a binary in
the image, and run one command at a time in the same sandbox, behind argument patterns, approvals, the vault and the
egress proxy (`GET /v1/clis` and `POST /v1/clis/{name}/run`; MCP `clis_list` and `cli_run`). One-shot commands only:
interactive sessions are not built yet. The image carries the vendors' agent CLIs (Claude Code, Codex, Gemini,
Cursor, Grok, Copilot; pinned in `image/clis.json`), listed without a deploy, each signed in from the person's phone
with `cli_login` (`POST /v1/clis/{name}/login`): it answers `needs_input` with the address to open, and
`program_continue` types back the code the page shows; and the `glyphh` CLI with the Glyphh SDK, which every program
imports without a `requirements.lock`. A program runs a CLI with `ctx.cli(name, args)` (its manifest's `clis`), in
that CLI's own sandbox and signed-in HOME.

A program can also be an **app** (`"kind": "app"`, [5.8](docs/architecture.md#58-apps)): what Claude made (a page,
a dashboard, a Markdown document, a React page, a diagram, a deck or a design as a PDF or an image), published from
Claude with `app_publish` (`POST /v1/apps`), reviewed in the Glyphh catalogue, and served at `/apps/<name>/` to the
org's people signed in to Glyphh, whose roles the app allows; a page's calls run the app's functions as the viewer.

The rrotor keeps one **graph**, its semantic layer ([5.9](docs/architecture.md#59-the-graph)): nodes, the edges
between them and the passages search reads, in SQLite on its own volume. A **pipeline** is a program on a schedule
whose manifest says `"graph": "write"`: plain Python that reads a source with a key from the vault and writes records
as nodes and edges with `ctx.graph` (`examples/hubspot_pipeline`). Apps and programs that say `"graph": "read"`
search and walk it, and so does Claude (`graph_search`, `graph_schema`, …; `GET /v1/graph`, `POST /v1/graph/{op}`).
A secret is typed by the person into a form Claude shows (`secrets_form`, an MCP App), never into the chat.

| Where | What |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | How it works: the modes, the sandbox, the API and MCP, the link, the licence, parts, CI |
| [`docs/self-hosting/`](docs/self-hosting/README.md) | Running your own: Docker Compose, Helm, Terraform; privileges, licensing, backups |
| `src/rrotord/`, `src/rrotor/` | The daemon; the program SDK every sandbox imports |
| `examples/` | `daily_sales`, a program with its tests; `hubspot_pipeline`, a pipeline into the graph with its tests; `cli_gh` and `cli_glyphh`, CLIs' manifests (placeholder pins) |
| `scripts/part.py` | Building and signing a part |
| `deploy/` | The self-hosting packages |
| `benchmarks/` | Ada (Ada Cloud's) measured against LLMs; kept here because other repos read it by path |

## Develop

```sh
uv sync
uv run ruff check src tests examples && uv run ruff format --check src tests examples
uv run mypy
uv run pytest      # 100% line and branch coverage of rrotor and rrotord
```

The sandbox's own tests run inside the image (`image/Dockerfile`, target `sandbox-test`); CI runs them
(`.github/workflows/ci.yml`).

## Benchmarks

These measure Ada, which runs in Ada Cloud, not in rrotor; they stay in this repo because other repos read them by
path.

| | Ada alone | Best LLM alone | Ada in front of the LLM |
|---|---|---|---|
| Loans: the bank's cost over 200 (lower is better) | **100** | 110 | |
| Support messages: 200 routed to 77 intents | 90.0% | 96.5% | **97.5%**, the LLM seeing 22%, at $0.22 against $300 |
| A session's memory: what a turn needs from earlier turns | recall by request 14%; recency 36%; a per-file ledger recovers it by construction | | history is 89% of every call's context: ~70% of the bill |
| Robots at the door: 2,000 web sessions, person or robot, from behaviour alone (gate's cost, lower is better) | **145.5**, 12 people blocked | 31 on the 200 sample, 6 people blocked | on sessions of 5+ requests: 99.6%, 3 people blocked in 1,322 |

- [`benchmarks/credit/`](benchmarks/credit/README.md): 200 real loan decisions, Ada against Claude on the same
  hold-out, with cost and time.
- [`benchmarks/banking77/`](benchmarks/banking77/README.md): a customer's message routed to one of 77 intents from
  its text alone; Ada and Claude together.
- [`benchmarks/session/`](benchmarks/session/README.md): what a Claude Code turn needs from the turns before it, on
  180 real sessions; why the `glyphh` plugin's file ledger.
- [`benchmarks/issues/`](benchmarks/issues/README.md): free-text issue triage, the counter-example.
- [`benchmarks/bots/`](benchmarks/bots/README.md): a web session, person or robot, from how it behaves; the gate
  at glyphh.ai's edge and what its first probe said.
