# rrotor with Helm

One rrotor per release: a StatefulSet of **one** replica with its own volume for `/data`, a Service, an Ingress at
your public host, and a Secret for the runtime's token. It is one box, as everywhere: there is no high availability
and no autoscaling, and the chart does not let you set replicas. For a second rrotor, register a second runtime and
install a second release. The chart is `deploy/helm/rrotor/`. A cluster of N replicas with a head is decided, not
built ([`specs/cluster.md`](../specs/cluster.md#12-the-packages)).

## What the cluster must allow

The sandbox inside rrotor needs seccomp and AppArmor unconfined and an unmasked `/proc`
([README](README.md#the-containers-privileges-and-why)). On Kubernetes an unmasked `/proc` is allowed only for a
pod in its own user namespace, so the pod sets:

```yaml
hostUsers: false                 # a user namespace for the pod: uid 0 inside is not root on the node
securityContext:                 # the container's
  seccompProfile: {type: Unconfined}
  appArmorProfile: {type: Unconfined}
  procMount: Unmasked
  readOnlyRootFilesystem: true
  allowPrivilegeEscalation: false
  capabilities: {drop: ["ALL"]}
```

That asks of the cluster:

- **Kubernetes 1.33 or later**, where user namespaces (`UserNamespacesSupport`) and `ProcMountType` are on by
  default. On 1.30 to 1.32 both feature gates must be turned on, on the API server and every kubelet.
- **Nodes that support user namespaces for pods:** Linux 6.3 or later with idmapped mounts on the volume's
  filesystem, and a runtime that supports them (containerd 2.0 or later, or CRI-O 1.25 or later).
- **Admission that allows it:** Pod Security Admission's `baseline` and `restricted` levels refuse an unconfined
  seccomp or AppArmor profile and an unmasked `/proc`, so the namespace needs the `privileged` level:

```sh
kubectl create namespace rrotor
kubectl label namespace rrotor pod-security.kubernetes.io/enforce=privileged
```

  A policy engine (Kyverno, Gatekeeper) needs the same exception for this namespace.
- **On Ubuntu 23.10 or later nodes:** `kernel.apparmor_restrict_unprivileged_userns=0` on the node (not a
  namespaced sysctl: set it in the node image or its bootstrap).

**What refuses it:** GKE Autopilot (it does not allow unconfined seccomp or AppArmor, or `procMount`), and any
cluster whose admission enforces `baseline` or `restricted` on the namespace. On GKE use a Standard cluster; on EKS
and AKS, node groups that meet the points above. If the pod is admitted but the sandbox still cannot start,
`GET /v1/health` says which layer and why, and rrotor refuses runs rather than run them unsandboxed.

## Install

Register the runtime on the platform (Getting started, Helm) for the runtime id and token, then:

```sh
helm install rrotor deploy/helm/rrotor -n rrotor \
  --set publicHost=rrotor.acme.com \
  --set runtime.id=<runtime id> \
  --set runtime.token=<runtime token> \
  --set ingress.className=nginx \
  --set ingress.certManagerClusterIssuer=letsencrypt
```

The notes print the connector, `https://rrotor.acme.com/mcp`. Point the host's DNS at the Ingress's address.

To keep the token out of Helm's values, make the Secret yourself and name it:

```sh
kubectl -n rrotor create secret generic rrotor-runtime \
  --from-literal=RROTOR_RUNTIME_ID=<runtime id> --from-literal=RROTOR_RUNTIME_TOKEN=<runtime token>
helm install rrotor deploy/helm/rrotor -n rrotor --set publicHost=rrotor.acme.com --set existingSecret=rrotor-runtime
```

## Values

| Value | Default | What |
|---|---|---|
| `publicHost` | (required) | The exact host rrotor answers at |
| `runtime.id`, `runtime.token` | | The runtime, unless `existingSecret` is set |
| `runtime.licenseKey` | | The licence key, optional (air-gapped; normally it arrives over the link or heartbeat), into the Secret as `GLYPHH_LICENSE_KEY`; `runtime.licence` is its old alias |
| `runtime.keysSha256` | `[]` | Air-gapped: the SHA-256 of each runtime key that may call it; with the link on, the keys come down the link |
| `existingSecret` | | A Secret with `RROTOR_RUNTIME_ID`, `RROTOR_RUNTIME_TOKEN` and optionally `GLYPHH_LICENSE_KEY` (`RROTOR_LICENCE` is still read) |
| `image.repository`, `image.tag` | `ghcr.io/glyphh-ai/rrotor`, the chart's `appVersion` | The image |
| `pullSecrets` | `[]` | Image pull secrets, for a mirror |
| `server` | `https://api.glyphh.ai` | The Glyphh server |
| `link` | `on` | `off` for air-gapped |
| `size` | `small` | `small`: 1 CPU (shared: 250m requested), 1 GiB, 3 GiB disk; `medium`: 2 CPU, 4 GiB, 10 GiB; `large`: 2 CPU, 8 GiB, 20 GiB |
| `resources` | | Requests and limits, replacing the preset's |
| `persistence.storageClass`, `persistence.size` | the default class, the preset's size | The volume for `/data` (`ReadWriteOnce`) |
| `ingress.enabled`, `ingress.className` | `true`, | The Ingress |
| `ingress.certManagerClusterIssuer` | | Adds `cert-manager.io/cluster-issuer`, so cert-manager issues the certificate |
| `ingress.tls.enabled`, `ingress.tls.secretName` | `true`, `<release>-rrotor-tls` | The certificate's Secret |
| `ingress.annotations` | | More annotations, for your controller |
| `podDisruptionBudget.enabled` | `false` | `minAvailable: 1`: a node drain then waits until you move rrotor by hand |
| `nodeSelector`, `tolerations`, `affinity` | | Where it runs: e.g. a node group that meets the requirements above |
| `drain.endpoint` | | Your OpenTelemetry collector, OTLP over HTTP (`OTEL_EXPORTER_OTLP_ENDPOINT`), e.g. `http://otel-collector.observability:4318`; empty: no drain |
| `drain.headers` | | `OTEL_EXPORTER_OTLP_HEADERS` (an API key), into the Secret; or that key in `existingSecret` |
| `drain.resourceAttributes`, `drain.serviceName` | | `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_SERVICE_NAME` |
| `drain.tracesSampler`, `drain.tracesSamplerArg` | | `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` (other `OTEL_*` variables, such as a CA certificate, in `extraEnv`) |
| `drain.signals`, `drain.buffer` | `logs,metrics,traces,audit`, `10000` | `RROTOR_DRAIN_SIGNALS`, `RROTOR_DRAIN_BUFFER` |
| `drain.payloads` | `off` | `RROTOR_LOG_PAYLOADS`: inputs and results in your drain |
| `serverLogs` | `off` | `RROTOR_SERVER_LOGS`: the same signals to Glyphh too; the chart refuses it with `link: "off"` |
| `audit.retentionDays`, `audit.maxMb` | `90`, `1024` | The audit log (Enterprise) on the volume ([Logs, audit and drains](README.md#logs-audit-and-drains)) |
| `extraEnv` | | More environment, e.g. `RROTOR_LOG_LEVEL` |

The probes (`startupProbe`, `livenessProbe`, `readinessProbe`) check `/healthz` and can be changed. The Ingress must
pass the `Host` header through (every common controller does) and keep a stream open: for ingress-nginx,
`nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"` keeps long MCP streams from being cut.

Without an Ingress (`ingress.enabled=false`), put your own load balancer in front of the Service on port 80: it
must end TLS for the public host and pass `Host` through.

## Air-gapped

Mirror the image into your registry (`image.repository`, `image.tag`, `pullSecrets`), take the licence key from
the platform, and install with `link=off` and the key in the Secret (`runtime.licenseKey`, or `GLYPHH_LICENSE_KEY` in
`existingSecret`). The certificate comes from your own issuer or a TLS Secret you make (`ingress.tls.secretName`).

## Upgrade, back up, remove

- **Upgrade:** `helm upgrade rrotor deploy/helm/rrotor -n rrotor --reuse-values --set image.tag=<version>`. The one
  pod is replaced; the volume stays.
- **Back up:** a VolumeSnapshot of the claim `data-rrotor-0`, or stop the pod
  (`kubectl -n rrotor scale statefulset/rrotor --replicas=0`, the one time you scale it) and copy the volume
  ([README, Backups](README.md#backups)), then `--replicas=1` again.
- **Remove:** `helm uninstall rrotor -n rrotor` keeps the claim, and so `/data`; delete `data-rrotor-0` to remove
  it. Delete the runtime on the platform when you are done with it.
