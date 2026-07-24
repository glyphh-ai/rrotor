# Contributing to rrotor

Thanks for your interest in rrotor — the open reference runtime for
[RotorSpec](SPEC.md). This project is developed in the open under the
[Apache-2.0](LICENSE) license, and we welcome issues, discussion, and pull
requests.

## Ground rules

- Be respectful. This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
- By contributing, you agree that your contributions are licensed under the
  Apache License, Version 2.0, per section 5 of the [LICENSE](LICENSE). No
  separate CLA is required — your pull request is your submission.
- Keep the [NOTICE](NOTICE) constraint in mind: the patent-pending HDC grounding
  method is deliberately **not** implemented here. The `hdc.map` step is
  abstracted/stubbed on purpose — please do not add a concrete implementation of
  the production grounding method.

## Development

Requires Node 20+.

```bash
npm install
npm run build      # tsc → dist/
npm run verify     # typecheck + lint + tests (run this before opening a PR)
```

Useful launchers (`make help` lists them all):

```bash
make tui           # full-screen TUI against a live model
make serve         # HTTP runtime
npm test           # vitest
```

## Pull requests

1. Fork the repo and create a topic branch from `main`.
2. Make your change. Add or update tests — the runtime is test-first, and CI runs
   `npm run verify`.
3. Keep changes focused; one logical change per PR.
4. Ensure `npm run verify` passes locally before pushing.
5. Open the PR with a clear description of the problem and the approach. Link any
   related issue.

## Reporting bugs & requesting features

Open a [GitHub issue](https://github.com/glyphh-ai/rrotor/issues). For anything
security-sensitive, follow [SECURITY.md](SECURITY.md) instead of filing a public
issue.

## Spec changes

Changes to the normative spec text ([SPEC.md](SPEC.md), the step catalog, or the
JSON Schema under `spec/`) are held to a higher bar than runtime changes. Open an
issue to discuss the motivation before sending a PR — a spec change that the
reference runtime can't yet honor should say so explicitly.
