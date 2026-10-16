# rrotor: the base image (docs/architecture.md, section 4).
#   docker buildx build -f image/Dockerfile --target base -t ghcr.io/glyphh-ai/rrotor:<version> .

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

# The sandbox's own tests, run inside the base image (they need bwrap, Landlock and libseccomp): CI only.
FROM base AS sandbox-test
USER root
COPY pyproject.toml uv.lock /src/
COPY tests /src/tests
COPY examples /src/examples
RUN uv pip install -q --no-cache --python /opt/rrotord/bin/python pytest pytest-asyncio \
 && mkdir -p /src/.data && chown -R rrotor:rrotor /src
USER rrotor
WORKDIR /src
CMD ["/opt/rrotord/bin/python", "-m", "pytest", "-m", "sandbox", "-o", "addopts=", "-p", "no:cacheprovider", "tests/sandbox"]
