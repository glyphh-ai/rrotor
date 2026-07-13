#!/usr/bin/env bash
# Provision a local Postgres + pgvector for exercising the pgvector stator against a
# REAL server (not just the in-process PGlite used in unit tests). Idempotent: safe
# to re-run. Prints the connection URL to export as ROTOR_TEST_PG_URL.
#
#   ./scripts/pg-setup.sh            # init + start + create db/extension
#   ROTOR_TEST_PG_URL=$(./scripts/pg-setup.sh --url-only) npm test
#
# Requires: postgresql-16 + postgresql-16-pgvector (apt). On Debian/Ubuntu:
#   apt-get install -y postgresql-16 postgresql-16-pgvector
set -euo pipefail

PGBIN="$(pg_config --bindir)"
PGDATA="${ROTOR_PGDATA:-/tmp/openrotor-pgdata}"
PGPORT="${ROTOR_PGPORT:-54329}"
PGDB="${ROTOR_PGDB:-openrotor_test}"
PGSOCK="${ROTOR_PGSOCK:-${PGDATA}-sock}"
URL="postgresql://postgres@localhost:${PGPORT}/${PGDB}?host=${PGSOCK}"

log() { echo "[pg-setup] $*" >&2; }

# Postgres refuses to run as root. When invoked as root (containers/CI), drop to the
# `postgres` system user; otherwise run inline as the current unprivileged user.
if [[ "$(id -u)" == "0" ]]; then
  RUN=(runuser -u postgres --)
  mkdir -p "${PGDATA}" "${PGSOCK}"
  chown -R postgres:postgres "${PGDATA}" "${PGSOCK}"
else
  RUN=()
fi
run() { "${RUN[@]}" "$@"; }

if [[ "${1:-}" != "--url-only" ]]; then
  # initdb once.
  if [[ ! -s "${PGDATA}/PG_VERSION" ]]; then
    log "initdb → ${PGDATA}"
    run "${PGBIN}/initdb" -D "${PGDATA}" -U postgres --auth=trust >/dev/null
  fi

  # Start if not already accepting connections.
  if ! run "${PGBIN}/pg_isready" -h "${PGSOCK}" -p "${PGPORT}" -q 2>/dev/null; then
    log "starting postgres on ${PGSOCK}:${PGPORT}"
    run "${PGBIN}/pg_ctl" -D "${PGDATA}" \
      -o "-p ${PGPORT} -k ${PGSOCK} -c listen_addresses=''" \
      -l "${PGDATA}/server.log" -w start >/dev/null
  fi

  # Create the database + pgvector extension (idempotent).
  if ! run "${PGBIN}/psql" -h "${PGSOCK}" -p "${PGPORT}" -U postgres -tAc \
        "SELECT 1 FROM pg_database WHERE datname='${PGDB}'" | grep -q 1; then
    log "creating database ${PGDB}"
    run "${PGBIN}/createdb" -h "${PGSOCK}" -p "${PGPORT}" -U postgres "${PGDB}"
  fi
  run "${PGBIN}/psql" -h "${PGSOCK}" -p "${PGPORT}" -U postgres -d "${PGDB}" -q \
    -c "CREATE EXTENSION IF NOT EXISTS vector;"
  log "ready: ${URL}"
fi

echo "${URL}"
