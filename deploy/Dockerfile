# rrotor runtime image — ONE image, TWO modes (the convergence seed).
#
#   ROTOR_MODE unset / "rotor" → the stateless, fungible rotor loop pod
#                                (SPEC.md §17.1): no run-critical state in the
#                                container, everything on the stator backend.
#   ROTOR_MODE=harness         → the HOSTED HARNESS SESSION POD: interactive
#                                Claude Agent SDK sessions (chat/work/code),
#                                gateway-routed model calls, frame stream.
#                                Per-session state (the sandbox workspace)
#                                lives under $HARNESS_HOME inside the pod —
#                                one pod ⇔ one session, provisioned by the
#                                control plane.
#
# The harness needs node + @anthropic-ai/claude-agent-sdk in the image; the SDK
# ships its `claude` CLI as a per-platform native binary via optionalDependencies,
# so `npm ci` on the build platform installs the matching linux binary and
# `npm prune --omit=dev` keeps it — nothing extra to COPY.
#
# Multi-stage: a `build` stage compiles TypeScript → dist, then a lean `runtime`
# stage copies only dist + production deps. Node 20-slim, non-root, HEALTHCHECK via
# node (no curl in the image), CMD runs the mode-dispatching pod entry.

# ---- build stage -----------------------------------------------------------
FROM node:22-slim AS build
WORKDIR /app

# Install ALL deps (incl. devDeps: typescript) against the lockfile, deterministically.
COPY package.json package-lock.json ./
RUN npm ci

# Compile. tsconfig emits ESM to dist/ (module: NodeNext).
COPY tsconfig.json ./
COPY src ./src
RUN npm run build

# Drop devDependencies so we copy only prod node_modules into the runtime stage.
RUN npm prune --omit=dev

# ---- runtime stage ---------------------------------------------------------
FROM node:22-slim AS runtime
WORKDIR /app

ENV NODE_ENV=production \
    PORT=8080 \
    ROTOR_PORT=8080

# node:20-slim ships a non-root `node` user (uid 1000). Run as it — no root in the
# serving container. Copy with ownership so the pod can read its own files.
COPY --from=build --chown=node:node /app/package.json ./package.json
COPY --from=build --chown=node:node /app/node_modules ./node_modules
COPY --from=build --chown=node:node /app/dist ./dist

USER node

EXPOSE 8080

# Liveness from inside the container, dependency-light (no curl/wget). Hits the same
# /healthz the kubelet probes; exit 0 = alive, non-zero = restart candidate.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD node -e "require('http').get('http://127.0.0.1:'+(process.env.PORT||8080)+'/healthz',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"

# The pod entry dispatches on ROTOR_MODE (default: the rotor loop server, so
# existing deploys keep their behavior; ROTOR_MODE=harness serves the hosted
# harness session pod). Both read $PORT.
CMD ["node", "dist/pod.js"]
