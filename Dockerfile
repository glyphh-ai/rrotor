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
# The browser-panel pod (ROTOR_MODE=panel, src/panel) streams a REAL Chromium to
# the client (video out + input back over CDP). It is a SEPARATE build target —
# Chromium, its X/font/render libraries, a virtual X server and a video encoder
# add several hundred MB, and the default `runtime` image above stays lean for
# the rotor/harness modes. Build it explicitly:
#
#     docker build --target panel -t rrotor-panel .
#
# and provision the browser-panel pods from THIS image, not the base one. The
# control plane decides which sessions get a panel pod; not every pod pays the
# Chromium cost.
#
# ── two video transports, in order of preference ─────────────────────────────
#   WEBRTC (v2)      Chromium runs HEADFUL on a per-panel Xvfb display; ffmpeg
#                    grabs that display and encodes H.264/VP8 in native code;
#                    werift (pure TS — no native peer build) does ICE/DTLS/SRTP.
#                    ~an order of magnitude cheaper on the wire than JPEG, and
#                    headful+real-GPU-stack behaves far better against bot checks
#                    than headless did.
#   SCREENCAST (v1)  CDP `Page.startScreencast` → base64 JPEG over the WS. Still
#                    here, still automatic: it is the fallback for clients whose
#                    UDP is blocked, clients without WebRTC, and any pod where
#                    Xvfb/ffmpeg are missing. Removing it would strand users.
#
# The pod picks per panel at runtime (panel/driver-select.ts) — `PANEL_XVFB=0`
# pins the v1 headless path, which is how the two are A/B measured.
#
# Density/cost note: the v1 driver ran ONE headless Chromium per pod with a
# context per panel; the v2 headful driver runs one Xvfb + one Chromium per panel
# (a window alone on its own display has an unambiguous, unoccludable capture
# rect and cannot be captured by another tenant). That is more RSS per panel and
# is why the panel tier is sized + metered separately (pod time is the billing
# rail — §7).
FROM runtime AS panel
USER root

# The browsers live at a SHARED path both root (which installs them) and the runtime
# `node` user can read — NOT root's private ~/.cache (mode 700), which the `node` user
# cannot reach → "Executable doesn't exist" at launch (the #1 panel deploy-breaker). Set
# PLAYWRIGHT_BROWSERS_PATH for BOTH the install below AND the running process.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# playwright-core is already in node_modules (a runtime dependency). Install the matching
# Chromium + the system libraries it needs. `--with-deps` pulls the apt packages. NOTE:
# install the FULL `chromium` bundle (NOT --no-shell) — it carries BOTH the headful binary
# the WebRTC path launches and the `chromium_headless_shell` that Playwright ≥1.49 resolves
# `headless: true` to for the fallback driver; excluding either breaks one of the two
# transports. Then make the browser dir readable by the non-root `node` user that runs the
# pod.
#
# xvfb    — the per-panel virtual X display headful Chromium renders into.
# ffmpeg  — the NATIVE encoder (x11grab → H.264/VP8 → RTP). Encoding must never happen
#           in JS; that was the whole cost problem with the JPEG path.
# The two together are the only reason this image is bigger than the v1 panel image.
RUN npx --yes playwright-core@1.61.1 install --with-deps chromium \
  && apt-get update \
  && apt-get install -y --no-install-recommends xvfb ffmpeg \
  && chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH" \
  && rm -rf /var/lib/apt/lists/*

# Xvfb's socket dir. Xvfb creates it when it can, but it runs as the non-root `node` user
# here, so it must exist and be writable before the first panel opens.
RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

# Container-Chromium hygiene: a real /dev/shm is tiny in containers (~64MB), which crashes
# heavy pages. The driver already passes --disable-dev-shm-usage; ALSO run the pod with a
# larger shared-memory segment — `docker run --shm-size=1g …`, or on Fly mount a tmpfs at
# /dev/shm (deploy/fly.panel.toml). ~1GB+ for heavy pages.
ENV ROTOR_MODE=panel

# WebRTC needs UDP the client can actually reach. Inside a container (and behind Fly's
# anycast proxy) the pod's own address is not that address, so ICE is pinned to a known
# port range and told what to advertise:
#   PANEL_ICE_PORT_RANGE   the UDP range to bind + publish  (default: ephemeral)
#   PANEL_ICE_HOST_IPS     address(es) to advertise as host candidates
#   PANEL_ICE_TCP=1        also offer ICE-TCP, for networks that drop UDP entirely
# None of them is required: with no reachable candidate the panel simply falls back to
# the JPEG screencast.
EXPOSE 41500-41600/udp

# Chromium runs as the non-root `node` user (matches the base image's USER).
USER node

# Same entry (`rrotor serve`), but ROTOR_MODE=panel routes to the panel server.
CMD ["node", "dist/cli.js", "serve"]
