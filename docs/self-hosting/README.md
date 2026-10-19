# Self-hosting rrotor

rrotor is the runtime your Ada models run in ([architecture](../architecture.md)). Glyphh hosts it on Pro, Team
and Enterprise; you can also run it yourself, on your own machine, cluster or cloud account, at your own address,
on every plan, free and with no limit on runtimes or precedents. It is the same image either way,
`ghcr.io/glyphh-ai/rrotor:<version>` (and `:latest`), with the Ada part built in; the licence, not the image, says
whether Ada runs.

A self-hosted rrotor is **one box**: one container, one `/data`, one public host. There is no high availability and
no autoscaling; a second copy of the same runtime is not a replica, it takes the link over from the first.

## The three ways

| | What you run | What it needs | Guide |
|---|---|---|---|
| **Docker Compose** | rrotor and Caddy (HTTPS) on one machine: `docker compose up -d` | A Linux machine with Docker and Compose v2, ports 80 and 443 reachable, a DNS name pointing at it | [compose.md](compose.md), `deploy/compose/` |
| **Helm** | A StatefulSet of one replica with its own volume, a Service and an Ingress | Kubernetes 1.33 or later that allows the sandbox's privileges (below), an Ingress controller, a storage class; cert-manager if you want it to issue the certificate | [helm.md](helm.md), `deploy/helm/rrotor/` |
| **Terraform** | One VM on AWS or Google Cloud running the Compose package, `/data` on its own disk | An AWS account or a Google Cloud project, and a DNS zone (Route 53 or Cloud DNS) or a name you point yourself | [terraform.md](terraform.md), `deploy/terraform/` |

Each starts the same way:

1. **Register the runtime** on the platform (`https://platform.glyphh.ai`, Getting started): a name and the public
   host it will answer at, e.g. `rrotor.acme.com`. The platform shows the runtime id and token **once**, and the
   snippet for the way you chose, filled in.
2. **Deploy** with that snippet.
3. **Add the connector** in Claude (Settings, Connectors) or any MCP client: `https://<public host>/mcp`. Signing in
   to Glyphh there is enough; a runtime key works too.

## The environment

What rrotor reads in `self` mode. Each package sets these for you.

| Variable | Required | Meaning |
|---|---|---|
| `RROTOR_MODE=self` | yes | A self-hosted rrotor |
| `RROTOR_RUNTIME_ID`, `RROTOR_RUNTIME_TOKEN` | yes, together | From registering the runtime on the platform. The token is this rrotor's identity to Glyphh: keep it secret |
| `RROTOR_PUBLIC_HOST` | yes | The exact host it answers at, e.g. `rrotor.acme.com`. Any other `Host` is refused (421 `E_HOST`) |
| `RROTOR_SERVER` | no, default `https://api.glyphh.ai` | The Glyphh server |
| `GLYPHH_LICENSE_KEY` | no | The licence key, for air-gapped use (normally it arrives over the link or the heartbeat; without one, Community's limits). `RROTOR_LICENCE` is its alias; also read from `/etc/rrotor/license.key` or `/data/licence.token`, at start, hourly and on `SIGHUP` (`PUT /v1/licence` is for a local rrotor only) |
| `RROTOR_LINK` | no, default `on` | `off` for air-gapped: no link and no heartbeat; rrotor runs on the offline licence alone |
| `RROTOR_KEYS_SHA256` | air-gapped | The SHA-256 of each runtime key that may call it, comma-separated (`printf %s "$KEY" \| sha256sum`). With the link on, the keys come down the link and this is not needed. With the link off there is no Glyphh sign-in either: runtime keys are the only way in |
| `RROTOR_DATA` | no, default `/data` | The persistent volume. One rrotor writes it, never two |

rrotor listens on port **7777**, plain HTTP: TLS is ended in front of it (Caddy, an Ingress, a load balancer), which
must pass the `Host` header through unchanged. `GET /healthz` is open and answers while the process is up;
`GET /v1/health` (authenticated) reports the link, each sandbox layer and the licence.

## The container's privileges, and why

Every model run is sandboxed inside rrotor: bubblewrap gives it its own user, mount, PID and network namespaces,
Landlock limits the files it can touch, a seccomp filter refuses the dangerous system calls, and an egress proxy is
its only way out ([architecture, section 4](../architecture.md#4-rrotor-inside)). Making namespaces and mounting a
fresh `/proc` from inside a container is exactly what a container runtime's defaults forbid, so rrotor's container
needs three of them lifted:

| Setting | Docker | Kubernetes | Why |
|---|---|---|---|
| seccomp unconfined | `--security-opt seccomp=unconfined` | `seccompProfile: Unconfined` | The default profile refuses `unshare` and `clone` with new namespaces |
| AppArmor unconfined | `--security-opt apparmor=unconfined` | `appArmorProfile: Unconfined` | The default profile refuses mounts, which bwrap needs for the run's filesystem |
| `/proc` unmasked | `--security-opt systempaths=unconfined` | `procMount: Unmasked` (with `hostUsers: false`) | A runtime masks parts of `/proc`; the kernel then refuses to mount a new `/proc` for the run's PID namespace |

On Ubuntu 23.10 and later the host also needs `kernel.apparmor_restrict_unprivileged_userns=0` (or an AppArmor
profile that lets the container make user namespaces); without it bwrap cannot bring up the run's loopback. The
Terraform modules set it; the Compose guide says how.

What is **not** asked for: no `--privileged`, no added capabilities, no host network, no host paths. rrotor runs as
an unprivileged user (uid 10001) on a read-only root filesystem, and its own seccomp filter, set with
`no_new_privs`, holds every run more tightly than the runtime's default profile would. If a layer cannot work,
`GET /v1/health` names it and rrotor refuses runs (`E_SANDBOX_UNAVAILABLE`) rather than run them unsandboxed.

## What it reaches, outbound

| To | Why | When |
|---|---|---|
| `api.glyphh.ai:443` | The link (a WebSocket, outbound only), the daily licence heartbeat, and checking a Glyphh sign-in | `RROTOR_LINK=on` |
| `ghcr.io:443` | Pulling the image | Installing and upgrading |
| Let's Encrypt (`acme-v02.api.letsencrypt.org:443`) | Caddy's certificate | Compose and Terraform, unless you bring your own |
| `github.com:443` | Fetching a model's commit when one is deployed from its repo | Deploys from git |
| The hosts a model declares | A run's own egress, port 443 only, through rrotor's proxy | Runs whose manifest asks for them |

Nothing connects in to rrotor except your callers on 443 (and 80 for the certificate and the redirect). The
Glyphh server never connects to it: everything it sends comes down the link rrotor opened. What crosses that link,
and what the heartbeat carries, is in [licensing.md](licensing.md).

## Licensing

One signed licence, by one of three paths: the **link** (a 24 hour licence on each connect), the **heartbeat** (a
small HTTPS call every 24 hours, for networks that block WebSockets), or **offline** (a licence of up to a year,
for air-gapped networks). Each holds for 14 days past its expiry. The exact heartbeat, and what is never sent, are
in [licensing.md](licensing.md).

## Upgrades

A new version is a new image tag; `/data` carries over. Pin a version in production rather than following
`latest`, and back up `/data` before you move.

- Compose: set `RROTOR_VERSION` in `.env`, then `docker compose pull && docker compose up -d`.
- Helm: `helm upgrade <release> deploy/helm/rrotor --reuse-values --set image.tag=<version>`.
- Terraform: set `rrotor_version` and apply. The VM is replaced; the data disk is detached and attached to the new
  one.

rrotor stops for the moment it is replaced: a run in flight does not survive it, as on any restart, so upgrade when
nothing is running. Go forwards only: to go back, restore the backup you took before.

## Backups

Everything rrotor keeps is on `/data`: the runtime's id and token (`runtime.json`), each model's deployed versions,
its own files and data (Ada's precedent store), each run's record and log, the parts installed, the licence held.
Back that up and you can rebuild rrotor anywhere.

- **The simplest consistent copy:** stop rrotor, copy the volume, start it.
  `docker compose stop rrotor`, then `docker run --rm -v rrotor_rrotor-data:/data:ro -v "$PWD":/out debian:trixie-slim tar czf /out/rrotor-data.tar.gz -C /data .`,
  then `docker compose start rrotor`.
- **Disk snapshots:** an EBS snapshot (`data_volume_id`), a Compute Engine snapshot (`data_disk`), or a CSI
  VolumeSnapshot of the claim `data-<name>-0` (`data-rrotor-0` for a release named `rrotor`). A snapshot of a running rrotor is crash-consistent: the
  link's outbox replays what was not acknowledged, and a run that was in flight is lost.
- **A backup holds the runtime token.** Keep it as you keep the token. Restoring it on a second machine while the
  first still runs makes two of the same runtime: the newer link takes over, the older stops (`replaced`).
- **A lost token:** rotate it on the platform and put the new one in the environment; the data stays.
