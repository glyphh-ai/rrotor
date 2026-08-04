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

# ── builder ──────────────────────────────────────────────────────────────────
FROM node:20-bookworm-slim AS builder
WORKDIR /app

# Toolchain to compile better-sqlite3's native binding. Not in the final image.
RUN apt-get update \
  && apt-get install -y --no-install-recommends python3 make g++ \
  && rm -rf /var/lib/apt/lists/*

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

# Compiled deps + build output from the builder (no toolchain in this layer).
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

# ── panel (browser-streaming) image ───────────────────────────────────────────
#
# The browser-panel pod (ROTOR_MODE=panel, src/panel) streams a REAL headless
# Chromium to the client (CDP screencast over WS + input back). Chromium + its
# X/font/render system libraries add ~400–500MB to the image and want shared
# memory, so this is a SEPARATE build target — the default `runtime` image above
# stays lean for the rotor/harness modes. Build it explicitly:
#
#     docker build --target panel -t rrotor-panel .
#
# and provision the browser-panel pods from THIS image, not the base one. The
# control plane decides which sessions get a panel pod; not every pod pays the
# Chromium cost.
#
# Density/cost note (spike finding): headless Chromium is ~120–200MB RSS idle and
# more per active tab; a panel pod is heavier than a rotor/harness pod and should
# be sized + metered accordingly (pod time is the billing rail — §7).
FROM runtime AS panel
USER root

# The browsers live at a SHARED path both root (which installs them) and the runtime
# `node` user can read — NOT root's private ~/.cache (mode 700), which the `node` user
# cannot reach → "Executable doesn't exist" at launch (the #1 panel deploy-breaker). Set
# PLAYWRIGHT_BROWSERS_PATH for BOTH the install below AND the running process.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# playwright-core is already in node_modules (a runtime dependency). Install the matching
# Chromium + the system libraries headless Chromium needs. `--with-deps` pulls the apt
# packages. NOTE: install the FULL `chromium` bundle (NOT --no-shell): the driver launches
# with `headless: true`, which Playwright ≥1.49 resolves to the `chromium_headless_shell`
# binary — excluding the shell makes the launch fail. Then make the browser dir readable
# by the non-root `node` user that actually runs the pod.
RUN npx --yes playwright-core@1.61.1 install --with-deps chromium \
  && chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH" \
  && rm -rf /var/lib/apt/lists/*

# Container-headless-Chromium hygiene: a real /dev/shm is tiny in containers (~64MB), which
# crashes heavy pages. The driver already passes --disable-dev-shm-usage; ALSO run the pod
# with a larger shared-memory segment — `docker run --shm-size=1g …`, or on Fly mount a
# tmpfs at /dev/shm (deploy/fly.panel.toml). ~1GB+ for heavy pages.
ENV ROTOR_MODE=panel

# Chromium runs as the non-root `node` user (matches the base image's USER).
USER node

# Same entry (`rrotor serve`), but ROTOR_MODE=panel routes to the panel server.
CMD ["node", "dist/cli.js", "serve"]
