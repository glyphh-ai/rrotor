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

# Best-effort: provision a real Postgres + pgvector so the pgvector stator tests run
# against a live server (not just PGlite). Never fail the session if it can't —
# ROTOR_TEST_PG_URL simply stays unset and those tests skip.
if command -v pg_config >/dev/null 2>&1; then
  if ! ls /usr/share/postgresql/*/extension/vector.control >/dev/null 2>&1; then
    apt-get install -y "postgresql-$(pg_config --version | grep -oE '[0-9]+' | head -1)-pgvector" >/dev/null 2>&1 || true
  fi
  if URL="$(bash scripts/pg-setup.sh 2>/dev/null)"; then
    echo "[session-start] ROTOR_TEST_PG_URL=$URL" >&2
    echo "export ROTOR_TEST_PG_URL='$URL'" >> "$HOME/.bashrc" 2>/dev/null || true
  fi
fi
