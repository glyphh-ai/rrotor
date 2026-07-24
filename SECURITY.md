# Security Policy

## Reporting a vulnerability

Please do **not** open a public GitHub issue for security vulnerabilities.

Report privately in one of two ways:

1. **GitHub private vulnerability reporting** — use the "Report a vulnerability"
   button under the repository's **Security** tab (preferred).
2. **Email** — [security@glyphh.ai](mailto:security@glyphh.ai).

Please include: a description of the issue, the affected version or commit, steps
to reproduce, and the impact you observed. We aim to acknowledge reports within
5 business days and to keep you informed as we investigate.

## Scope

This repository is the **open reference runtime** for RotorSpec. The
patent-pending HDC grounding method and Glyphh's production grounding
implementation are **not** part of this repository (see [NOTICE](NOTICE)) — vulnerabilities in those are out of scope here and should be directed to
[security@glyphh.ai](mailto:security@glyphh.ai).

When reporting, note that the default model lane is a deterministic **stub**; if a
finding depends on a live model or a specific provider endpoint, say so.

## Disclosure

We follow coordinated disclosure. Please give us a reasonable window to release a
fix before any public disclosure, and we'll credit you in the release notes unless
you prefer otherwise.
