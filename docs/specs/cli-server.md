# The CLI server: sandboxed command-line tools Claude drives through rrotor

**Status: proposed (2026-10-31), not built.** For the founder's review before any code.

## What it is

rrotor already runs **programs**: a `glyphh.json` manifest, Python functions, a sandbox (bwrap, seccomp, Landlock),
an egress proxy that allows only declared hosts, the secrets vault, schedules, approvals and an audit log, reached
from Claude over rrotor's local REST and MCP. The CLI server is the second thing rrotor hosts: **command-line tools**
(the glyphh CLI, `gh`, `stripe`, `aws`, a team's own scripts) installed into the runtime and run inside that same
sandbox, so Claude can use a tool without it touching the person's machine, their files, or the open network.

It is to CLIs what an MCP server is to tools, but generic: one rrotor hosts many CLIs, and adding one is a manifest,
not new server code.

## A CLI is a kind of program

Same manifest, same deploy, same sandbox, vault, egress proxy, approvals, limits and audit. One new field says what
runs:

```json
{
  "name": "github",
  "kind": "cli",
  "cli": {
    "install": {"url": "https://github.com/cli/cli/releases/download/v<version>/gh_<version>_linux_amd64.tar.gz", "sha256": "<checksum>"},
    "command": "gh",
    "allow": ["pr *", "issue *", "repo view *", "api repos/*"],
    "env": {"GH_TOKEN": "secret:GH_TOKEN"},
    "home": true,
    "interactive": false
  },
  "hosts": ["api.github.com", "github.com"],
  "secrets": ["GH_TOKEN"],
  "approvals": ["pr merge *", "repo delete *"],
  "limits": {"seconds": 120, "memory_mb": 512}
}
```

- **install**: pinned to an exact version and checksum, from npm, PyPI, a release tarball URL, or a binary already in
  the image. Installed once per version under `/data/programs/<name>/<version>/`; a new version is a new deploy and
  rolls back like any program.
- **allow**: argument patterns the CLI may be called with (glob over the joined arguments). Anything else is
  refused, `E_CLI_DENIED`. Empty means any arguments.
- **approvals**: patterns that pause the run for a person's approval first, as a program's `ctx.ask_approval` does.
- **env**: environment for the process; `secret:NAME` injects a vault secret, never written to disk or logs (run
  logs already mask secret values).
- **home**: a persistent, private `HOME` for the CLI (its config and any login it keeps, e.g. `gh auth login`),
  kept across calls on the runtime's volume, never shared with another program.
- **hosts**: the egress allow-list, exactly as for programs.

## How Claude uses it

MCP and REST, beside the program tools:

| tool | what it does |
|---|---|
| `clis_list` | the installed CLIs, their version, allowed patterns and whether each needs approval |
| `cli_run {cli, args, stdin?, files?}` | one command in the sandbox; returns exit code, stdout and stderr (capped, the rest in the run log); a pattern that needs approval returns `needs_approval` like any run |
| `cli_start` / `cli_send` / `cli_read` / `cli_stop` | an interactive session on a pseudo-terminal, for tools that prompt (only with `"interactive": true`) |

Every call is a run: it shows in `run_status`, `run_log` and the audit log (the command line with secrets masked).
Files go in and out through a per-call working folder, not the person's filesystem.

## What it is not

- Not a shell: there is no way to run a command the manifest does not name.
- Not root and not the host: the same unprivileged sandbox programs use; no Docker socket.
- Nothing goes through the Glyphh server: it only licenses the runtime.

## Build order

1. `kind: "cli"` in the manifest and its checks; install from a pinned npm/PyPI/tarball with checksum.
2. `cli_run` (one-shot), allow and approval patterns, env from the vault, the private `HOME`, audit.
3. Interactive sessions on a pseudo-terminal.
4. The glyphh CLI as the first packaged CLI, with an example manifest for `gh`.

## Open questions for the founder

1. CLIs as a **kind of program** (one manifest, one set of tools, recommended) or a separate first-class thing?
2. **Interactive sessions** in the first release, or one-shot commands first?
3. **Databases** for programs (`ctx.db`, today a stub): start with SQLite in the program's data folder (no extra
   process, recommended), then a Postgres part; or Postgres first?
