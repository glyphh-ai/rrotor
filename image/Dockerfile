# rrotor: the base image (docs/architecture.md, section 4), and the base plus the browser part (section 12).
#   docker buildx build -f image/Dockerfile --target base    -t ghcr.io/glyphh-ai/rrotor:<version> .
#   docker buildx build -f image/Dockerfile --target browser -t ghcr.io/glyphh-ai/rrotor:<version>-browser .

FROM debian:trixie-slim AS os
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv bubblewrap libseccomp2 util-linux git ca-certificates tini libstdc++6 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=node:22-trixie-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /usr/local/bin/uv

FROM os AS build
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --no-hashes -o /tmp/requirements.txt >/dev/null \
 && uv venv -q /opt/rrotord --python /usr/bin/python3 \
 && uv pip install -q --no-cache --python /opt/rrotord/bin/python -r /tmp/requirements.txt
COPY src ./src
RUN uv pip install -q --no-cache --no-deps --python /opt/rrotord/bin/python . \
 && /opt/rrotord/bin/python -c "import rrotord, rrotor"

FROM os AS base
ARG VERSION=dev
LABEL org.opencontainers.image.source="https://github.com/glyphh-ai/rrotor" \
      org.opencontainers.image.description="rrotor: Glyphh's per-person runtime (base)" \
      org.opencontainers.image.version="${VERSION}"
COPY --from=build /opt/rrotord /opt/rrotord
RUN useradd --uid 10001 --user-group --create-home --home-dir /home/rrotor --shell /usr/sbin/nologin rrotor \
 && mkdir -p /data && chown rrotor:rrotor /data
ENV PATH=/opt/rrotord/bin:/usr/local/bin:/usr/bin:/bin \
    RROTOR_DATA=/data RROTOR_LOCAL_PORT=7777 PYTHONDONTWRITEBYTECODE=1
USER rrotor
VOLUME /data
EXPOSE 7777
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD ["/opt/rrotord/bin/python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:7777/healthz', timeout=2)"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["rrotord"]

# The browser part, baked in until the part manager installs it on /data/parts/browser (docs, section 12).
FROM base AS browser
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium fonts-liberation fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*
ENV RROTOR_CHROMIUM=/usr/bin/chromium
USER rrotor
