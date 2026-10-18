# rrotor with Docker Compose

One machine: rrotor, and Caddy in front of it for HTTPS. The full guide is `docs/self-hosting/compose.md`.

1. Register the runtime on the platform (Getting started, Docker Compose) and copy the `.env` it shows; or copy
   `.env.example` to `.env` and fill in the runtime id, token and public host.
2. Point the public host's DNS at this machine, with ports 80 and 443 open to it.
3. Start it:

```sh
docker compose up -d
```

The Glyphh connector is then at `https://<public host>/mcp`. Linux hosts on Ubuntu 23.10 or later first need
`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` (kept in `/etc/sysctl.d/`), or the sandbox cannot start.

**Air-gapped:** put the offline licence in `.env` as `RROTOR_LICENCE`, your certificate and key in `certs/tls.crt` and
`certs/tls.key`, and start with the overlay, which turns the link off:

```sh
docker compose -f docker-compose.yml -f docker-compose.airgap.yml up -d
```
