# rrotor

The runtime a person's Ada models run in: one locked-down, headless Linux image that deploys a model by commit, runs
each call in a sandbox, and answers over REST and MCP at its own address. Hosted by Glyphh at
`https://<machine>.glyphh.app/mcp`, or self-hosted at `https://<host>/mcp`; either is added to Claude as a
connector (Glyphh sign-in or a runtime key) and called by apps with the `glyphh` SDKs (`glyphh-ai/glyphh`). Image:
`ghcr.io/glyphh-ai/rrotor`.

| Where | What |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | How it works: the modes, the sandbox, the API and MCP, the link, Ada, the licence, parts, CI |
| [`docs/self-hosting/`](docs/self-hosting/README.md) | Running your own: Docker Compose, Helm, Terraform; privileges, licensing, backups |
| `src/rrotord/`, `src/rrotor/` | The daemon; the model SDK every sandbox imports |
| `examples/` | `ada_incidents`, `ada_tickets` (Ada models) and `daily_sales` (the base alone), each with tests |
| `parts/ada/`, `scripts/part.py` | The Ada part's recipe; building and signing a part |
| `deploy/` | The self-hosting packages |
| `benchmarks/` | Ada measured against LLMs and classic models |

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

| | Ada alone | Best LLM alone | Ada in front of the LLM |
|---|---|---|---|
| Loans: the bank's cost over 200 (lower is better) | **100** | 110 | |
| Support messages: 200 routed to 77 intents | 90.0% | 96.5% | **97.5%**, the LLM seeing 22%, at $0.22 against $300 |
| A session's memory: what a turn needs from earlier turns | recall by request 14%; recency 36%; a per-file ledger recovers it by construction | | history is 89% of every call's context: ~70% of the bill |

- [`benchmarks/credit/`](benchmarks/credit/README.md): 200 real loan decisions, Ada against Claude and a classic
  model on the same hold-out, with cost and time.
- [`benchmarks/banking77/`](benchmarks/banking77/README.md): a customer's message routed to one of 77 intents from
  its text alone; Ada and Claude together.
- [`benchmarks/session/`](benchmarks/session/README.md): what a Claude Code turn needs from the turns before it, on
  180 real sessions; why the `glyphh` plugin's file ledger.
- [`benchmarks/issues/`](benchmarks/issues/README.md): free-text issue triage, the counter-example.
