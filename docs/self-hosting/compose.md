# rrotor with Docker Compose

One Linux machine runs rrotor and [Caddy](https://caddyserver.com) in front of it; Caddy obtains and renews the
HTTPS certificate for your public host by itself. The files are in `deploy/compose/`.

## What you need

- A Linux machine (amd64 or arm64) with Docker Engine and Compose v2. For the sizes: 1 CPU and 2 GiB for `small`,
  2 CPU and 8 GiB for `medium`, 2 CPU and 16 GiB for `large` (rrotor's limit, plus room for the OS, Docker and
  Caddy), and the disk for `/data` (3, 10 or 20 GiB).
- A DNS name, e.g. `rrotor.acme.com`, with an A (or AAAA) record pointing at the machine.
- Ports 80 and 443 open to it from the internet: 443 for callers, 80 for Let's Encrypt's check and the redirect to
  HTTPS.
- Outbound HTTPS to `api.glyphh.ai`, `ghcr.io` and Let's Encrypt ([README](README.md#what-it-reaches-outbound)).
- On Ubuntu 23.10 or later (24.04 included), user namespaces allowed:

```sh
echo 'kernel.apparmor_restrict_unprivileged_userns = 0' | sudo tee /etc/sysctl.d/60-rrotor-userns.conf
sudo sysctl --system
```

## Start it

1. Register the runtime on the platform (Getting started, Docker Compose). Copy the `.env` it shows; the token is
   shown once.
2. Put the files on the machine:

```sh
git clone --depth 1 https://github.com/glyphh-ai/rrotor.git && cd rrotor/deploy/compose
cp .env.example .env    # or paste the platform's; fill in RROTOR_RUNTIME_ID, RROTOR_RUNTIME_TOKEN, RROTOR_PUBLIC_HOST
chmod 600 .env
```

3. Start:

```sh
docker compose up -d
```

4. Check it: `docker compose ps` shows rrotor `healthy`; `docker compose logs caddy` shows the certificate obtained;
   `curl https://<public host>/healthz` answers. Then add `https://<public host>/mcp` as a connector in Claude.

## What is in the files

| File | What |
|---|---|
| `docker-compose.yml` | rrotor (`RROTOR_MODE=self`, `.env`, `/data` on the named volume `rrotor-data`, the three security options, a read-only root with a tmpfs `/tmp`, restart unless stopped, a health check on `/healthz`) and Caddy (ports 80 and 443, its certificates on `caddy-data`) |
| `Caddyfile` | Serve `RROTOR_PUBLIC_HOST` over HTTPS and pass every request to `rrotor:7777` |
| `.env.example` | The settings: runtime id and token, public host, version, size |
| `docker-compose.airgap.yml`, `Caddyfile.airgap` | The air-gapped overlay (below) |

rrotor's port is not published: only Caddy reaches it, on the Compose network. To put your own load balancer or
reverse proxy in front instead of Caddy, remove the `caddy` service and publish rrotor's 7777 to it on a private
address; it must end TLS and pass the `Host` header through.

## Size

Set `RROTOR_CPUS` and `RROTOR_MEMORY` in `.env`: `1` and `1g` (small), `2` and `4g` (medium), `2` and `8g` (large).

## Air-gapped

No route to Glyphh, Let's Encrypt or a registry. rrotor runs on an offline licence, with the link off; Caddy
serves a certificate you supply.

1. **Bring the images in.** On a machine with access, pin a version and save both images:

```sh
docker pull ghcr.io/glyphh-ai/rrotor:<version> && docker pull caddy:2
docker save ghcr.io/glyphh-ai/rrotor:<version> caddy:2 | gzip > rrotor-images.tar.gz
```

   Carry `rrotor-images.tar.gz` and `deploy/compose/` across, then `docker load < rrotor-images.tar.gz`, and set
   `RROTOR_VERSION=<version>` in `.env` (not `latest`).

2. **The licence key.** Take it from the platform ([licensing.md](licensing.md#air-gapped))
   and put it in `.env` as `GLYPHH_LICENSE_KEY=<key>`.
3. **The certificate.** Your certificate for the public host, its full chain in `certs/tls.crt` and its key in
   `certs/tls.key` (readable by the container; Caddy runs as root in its image).
4. **Start with the overlay**, which sets `RROTOR_LINK=off` and points Caddy at `certs/`:

```sh
docker compose -f docker-compose.yml -f docker-compose.airgap.yml up -d
```

With the link off there is no Glyphh sign-in to check, so callers use runtime keys. Renew the licence before it
expires: put the next key in `GLYPHH_LICENSE_KEY` and `docker compose -f docker-compose.yml -f docker-compose.airgap.yml up -d`.

## Upgrade, back up, remove

- **Upgrade:** set `RROTOR_VERSION` in `.env`, then `docker compose pull && docker compose up -d`.
- **Back up:** [README, Backups](README.md#backups). The volume is `rrotor_rrotor-data`.
- **Remove:** `docker compose down` keeps the volumes; `docker compose down -v` deletes `/data` and Caddy's
  certificates too. Delete the runtime on the platform when you are done with it.
