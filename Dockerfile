# syntax=docker/dockerfile:1
#
# rrotor — the loop-execution runtime, packaged as a container so the fleet
# orchestrator can run ONE `rrotor serve` per session (a Fly machine) and reach it
# over HTTP (/run · /ws · /healthz). rrotor does no auth of its own — it runs in an
# isolated per-session container behind the orchestrator; the security boundary is
# the orchestrator + the network, and the injected stator/scope env.
#
# No secrets are baked in: env (PORT, ROTOR_STATOR_BACKEND, ROTOR_STATOR_URL, the
# per-session scope) is injected at runtime by the orchestrator.
#
#   builder  — install (with the toolchain better-sqlite3's native addon needs),
#              compile TS → dist, then prune dev deps.
#   runtime  — a slim, non-root image running `rrotor serve` on 8080.

# @glyphh/sdk is a `file:../sdk` dependency — outside this build context. Build
# with an extra named context pointing at the sibling checkout:
#   docker build --build-context sdk=../sdk -t rrotor:dev .
# The SDK is staged at /sdk (a sibling of /app) in BOTH stages, so npm's
# node_modules/@glyphh/sdk → ../../sdk symlink resolves at install AND runtime.

# ── builder ──────────────────────────────────────────────────────────────────
FROM node:20-bookworm-slim AS builder
WORKDIR /app

# Toolchain to compile better-sqlite3's native binding. Not in the final image.
RUN apt-get update \
  && apt-get install -y --no-install-recommends python3 make g++ \
  && rm -rf /var/lib/apt/lists/*

# The linked SDK (package.json + built dist — it has no runtime deps of its own).
COPY --from=sdk package.json /sdk/package.json
COPY --from=sdk dist /sdk/dist

COPY package.json package-lock.json ./
RUN npm ci

COPY tsconfig.json ./
COPY src ./src
RUN npm run build \
  && npm prune --omit=dev

# ── runtime ──────────────────────────────────────────────────────────────────
FROM node:20-bookworm-slim AS runtime
ENV NODE_ENV=production
WORKDIR /app

# The agent's WORKSPACE toolchain (distinct from the builder stage, which only
# compiles rrotor itself): git makes GitHub source a first-class cloud
# workspace (clone/branch/commit, git-URL npm deps); python3/make/g++ let a
# project's node-gyp deps compile; ca-certificates keeps both talking TLS.
RUN apt-get update \
  && apt-get install -y --no-install-recommends git python3 make g++ ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# The linked SDK again — node_modules/@glyphh/sdk is a symlink to /sdk.
COPY --from=sdk package.json /sdk/package.json
COPY --from=sdk dist /sdk/dist

# Compiled deps + build output from the builder (rrotor's own compile still
# happens only in the builder layer).
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/dist ./dist
# Bundled specs + built-in rotors the engine loads at runtime (package.json "files").
COPY spec ./spec
COPY rotors ./rotors
COPY package.json ./

# The HTTP runtime port (probes + /run + /ws). Override with PORT / -p.
EXPOSE 8080

# Liveness — /healthz is up when the event loop turns; never depends on downstream.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||8080)+'/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

# Drop root.
USER node

# One `rrotor serve` per container; the orchestrator injects env (stator + scope).
CMD ["node", "dist/cli.js", "serve"]
