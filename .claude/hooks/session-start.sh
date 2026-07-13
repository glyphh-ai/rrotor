#!/bin/bash
# SessionStart hook — installs Node dependencies so `npm run verify`
# (typecheck + lint + test) works in Claude Code on the web sessions.
# Idempotent and non-interactive; container state is cached after it completes.
set -euo pipefail

# Only run in the remote (web) environment; local sessions manage their own deps.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# `npm install` (not `npm ci`) so a warm container reuses node_modules.
npm install --no-audit --no-fund
