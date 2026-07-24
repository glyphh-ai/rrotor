# rrotor runtime image — a stateless, fungible rotor pod (SPEC.md §17.1).
#
# The image carries NO run-critical state: the Context, the append-only event
# history, and the caches all live in the external stator backend (docs/runtime.md
# §5.3). Any pod built from this image can serve any run — which is exactly what
# lets a Deployment of these pods scale horizontally behind one Service.
#
# Multi-stage: a `build` stage compiles TypeScript → dist, then a lean `runtime`
# stage copies only dist + production deps. Node 20-slim, non-root, HEALTHCHECK via
# node (no curl in the image), CMD runs the HTTP probe/run server.

# ---- build stage -----------------------------------------------------------
FROM node:20-slim AS build
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
FROM node:20-slim AS runtime
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

# The runtime instance: the HTTP probe/run server. `startServer` reads $PORT.
CMD ["node", "dist/server.js"]
