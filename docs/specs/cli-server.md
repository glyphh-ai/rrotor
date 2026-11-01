# The CLI server: sandboxed command-line tools Claude drives through rrotor

**Status: approved (2026-10-31); steps 1, 2, 4 and 5 built, step 3 (interactive sessions) not built.** The founder's
rulings: a CLI is a kind of program; one-shot commands first (interactive sessions later); program databases start
with SQLite (built: `ctx.db`). How it is built: [`architecture.md`, 5.7](../architecture.md#57-clis).

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
    "approvals": ["pr merge *", "pr close *"],
    "env": {"GH_TOKEN": "secret:GH_TOKEN"},
    "home": true,
    "interactive": false
  },
  "hosts": ["api.github.com", "github.com"],
  "secrets": ["GH_TOKEN"],
  "limits": {"seconds": 120, "memory_mb": 512}
}
```

As built, `approvals` sits in the `cli` block (a program's approvals are per function), and an install is kept under
the version's folder, `/data/programs/<name>/versions/<sha>/cli/`.

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

1. **Built.** `kind: "cli"` in the manifest and its checks; install from a pinned npm/PyPI/tarball (or an image
   binary) with checksum.
2. **Built.** `cli_run` (one-shot), allow and approval patterns, env from the vault, the private `HOME`, audit.
3. **Not built.** Interactive sessions on a pseudo-terminal (`"interactive": true` is refused, `E_MANIFEST`).
4. **Built.** The glyphh CLI as the first packaged CLI (`examples/cli_glyphh/`), with an example manifest for `gh`
   (`examples/cli_gh/`); their version and checksum pins are placeholders.
5. **Built (2026-11-01).** The vendors' agent CLIs in the image, pinned (Claude Code, Codex, Gemini, Cursor, Grok,
   Copilot), deployed at boot as built-in CLIs; and signing in from afar (`login`, `cli_login`): the sign-in command
   kept running on a pseudo-terminal across the person's wait, its address (and device code) answered as
   `needs_input`, the code they paste typed back into it by `program_continue`
   ([`architecture.md`, 5.7](../architecture.md#57-clis)). The glyphh CLI and the Glyphh SDK in the image; a
   program runs a CLI with `ctx.cli` (its manifest's `clis`), in the CLI's own sandbox, its approval patterns steps
   of the program's run ([`architecture.md`, 5.3](../architecture.md#53-the-sdk-rrotor)).

## The founder's answers (2026-10-31)

1. CLIs are a **kind of program** (one manifest, one set of tools).
2. **One-shot commands first**; interactive sessions later.
3. **Databases** start with SQLite in the program's data folder (`ctx.db`); a Postgres part later.
