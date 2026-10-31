# cli_gh

GitHub's `gh` as a CLI on rrotor (`"kind": "cli"`, `docs/architecture.md`, 5.7): installed at deploy from its release
tarball, run one command at a time in the sandbox.

**Placeholders:** the release (`v2.62.0`) and its `sha256` (64 zeros) are placeholders. Pin the release you want
and its checksum (the `gh_<version>_checksums.txt` asset of that release) before deploying; a wrong checksum is
refused, `E_CLI_CHECKSUM`, and nothing is unpacked.

- **allow:** `pr *`, `issue *`, `repo view *`, `api repos/*`; anything else is refused, `E_CLI_DENIED`. A pattern
  matches the whole argument line, so a trailing `*` also takes further flags (`api repos/x -X DELETE` matches
  `api repos/*`): write narrower patterns for what must not pass.
- **approvals:** `pr merge *`, `pr close *`, `issue close *` wait for the person's approval first.
- **env:** `GH_TOKEN` from the vault (`secret_set github GH_TOKEN ...`), never written to disk or logs.
- **home:** `gh`'s config, kept across calls on the runtime, private to this CLI.
- **hosts:** `api.github.com` and `github.com`; the install also reaches GitHub's release asset hosts.

Its test (`tests/daemon/test_cli_manifest.py`) validates the manifest only; nothing is downloaded.
