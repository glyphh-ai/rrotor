# cli_glyphh

The `glyphh` command, the first packaged CLI on rrotor (`"kind": "cli"`, `docs/architecture.md`, 5.7): the wheel of
the PyPI package `glyphh`, installed at deploy (found through PyPI's JSON API by its SHA-256, unpacked by rrotor, its
`glyphh` console script run with the image's Python), run one command at a time in the sandbox.

**Placeholders:** the version (`3.5.0`) and its `sha256` (64 zeros) are placeholders. Pin the version and the
SHA-256 of its `py3-none-any` wheel (PyPI shows it on the release's files page) before deploying. The package has no
dependencies; one that had would list them, hashed, in a `requirements.lock` beside `glyphh.json`.

- **allow:** none, so any arguments; no approvals.
- **env:** `GLYPHH_API_KEY` from the vault (`secret_set glyphh GLYPHH_API_KEY ...`).
- **home:** its sign-in and settings, kept across calls, private to this CLI.
- **hosts:** `ada.glyphh.ai` and `platform.glyphh.ai`; the install also reaches `pypi.org` and
  `files.pythonhosted.org`.

Its test (`tests/daemon/test_cli_manifest.py`) validates the manifest only; nothing is downloaded.
