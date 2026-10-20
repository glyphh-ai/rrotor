# rrotor with Docker Compose

> The public copy customers use lives in [glyphh-ai/glyphh/deploy](https://github.com/glyphh-ai/glyphh/tree/main/deploy) (compose, helm, terraform): every change to `deploy/` here must be made there too, kept in step.

One machine: rrotor, and Caddy in front of it for HTTPS. The full guide is `docs/self-hosting/compose.md`.

1. Register the runtime on the platform (Getting started, Docker Compose) and copy the `.env` it shows; or copy
   `.env.example` to `.env` and fill in the runtime id, token and public host.
2. Point the public host's DNS at this machine, with ports 80 and 443 open to it.
3. Start it:

```sh
docker compose up -d
```

The runtime's connector is then at `https://<public host>/mcp`. Linux hosts on Ubuntu 23.10 or later first need
`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` (kept in `/etc/sysctl.d/`), or the sandbox cannot start.

**Air-gapped:** put the licence key in `.env` as `GLYPHH_LICENSE_KEY`, your certificate and key in `certs/tls.crt` and
`certs/tls.key`, and start with the overlay, which turns the link off:

```sh
docker compose -f docker-compose.yml -f docker-compose.airgap.yml up -d
```

**A cluster** (a licence key with cluster sync; `RROTOR_CLUSTER_SECRET` in `.env`, `openssl rand -base64 32`): to
try one, three nodes and Caddy balancing over them on this machine,

```sh
docker compose -f docker-compose.cluster.yml up -d
```

For high availability, `docker-compose.node.yml` on each of several machines, with `RROTOR_NODE_ID`,
`RROTOR_CLUSTER_ADVERTISE` and `RROTOR_CLUSTER_PEERS` in each `.env`, port 7778 open between them only and your load
balancer in front of port 7777. See `docs/self-hosting/compose.md`, "A cluster".
