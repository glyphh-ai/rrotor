# rrotor with Terraform

One VM on AWS or Google Cloud, set up on its first boot to run the Docker Compose package
([compose.md](compose.md)) with `/data` on a disk of its own; or, with `node_count`, a [cluster](#a-cluster) of
them behind the cloud's load balancer. The modules are `deploy/terraform/aws/` and `deploy/terraform/gcp/`, each with
usage examples in `examples/basic/` and `examples/cluster/`.

## What each makes

| | AWS (`aws/`) | Google Cloud (`gcp/`) |
|---|---|---|
| Network | A VPC with one public subnet, or your `vpc_id` and `subnet_id` | A network and subnetwork, or your `network` and `subnetwork` |
| Firewall | A security group: 443 (TCP and UDP) and 80 in from `ingress_cidrs`, all out | A firewall rule: the same in; Compute Engine's default rule lets all out |
| The VM | EC2, Ubuntu 24.04 (Canonical's latest image at creation), IMDSv2 only, an encrypted 20 GiB root | Compute Engine, Ubuntu 24.04, Shielded VM, a 20 GB root |
| `/data` | A separate encrypted gp3 EBS volume | A separate balanced persistent disk |
| Address | An Elastic IP | A static external address |
| Identity | An IAM role and instance profile with `AmazonSSMManagedInstanceCore` only | A service account with log and metric writing only |
| A shell | Session Manager (`aws ssm start-session`); no SSH key, no port 22 | OS Login over Identity-Aware Proxy, with `allow_iap_ssh`; project SSH keys blocked |
| DNS, optional | An A record in `route53_zone_id` | An A record in `dns_managed_zone` |

On first boot cloud-init installs Docker and Compose from Ubuntu, sets `kernel.apparmor_restrict_unprivileged_userns=0`,
formats the data disk the first time only and mounts it at `/mnt/rrotor`, writes the Compose files and `.env` to
`/opt/rrotor` (`.env` readable by root only), and runs `docker compose up -d`. rrotor's `/data` and Caddy's
certificates are both on the data disk.

## Sizes

| `size` | rrotor's limit | `/data` | AWS | Google Cloud |
|---|---|---|---|---|
| `small` | 1 CPU, 1 GiB | 3 GiB (10 GB on Google Cloud, its smallest) | `t3.small` | `e2-small` |
| `medium` | 2 CPU, 4 GiB | 10 GiB | `t3.large` | `e2-standard-2` |
| `large` | 2 CPU, 8 GiB | 20 GiB | `r7i.large` | `e2-highmem-2` |

Each VM has rrotor's memory and room for Ubuntu, Docker and Caddy besides. `instance_type` (or `machine_type`) and
`data_disk_gb` override the size's.

## Use

Register the runtime on the platform (Getting started, Terraform) for the runtime id and token, then, for AWS:

```hcl
module "rrotor" {
  source = "git::https://github.com/glyphh-ai/rrotor.git//deploy/terraform/aws?ref=<release>"

  public_host     = "rrotor.acme.com"
  runtime_id      = var.runtime_id
  runtime_token   = var.runtime_token # sensitive
  size            = "small"
  rrotor_version  = "<version>"
  route53_zone_id = "Z0123456789EXAMPLE" # optional
}

output "connector_url" { value = module.rrotor.connector_url }
```

and for Google Cloud the same with `//deploy/terraform/gcp`, `project`, `region`, `zone` and, optionally,
`dns_managed_zone`. The modules read `deploy/compose/` and `deploy/terraform/shared/` by relative path, so use them
from this repository (a `git::` source, as above), not copied out alone.

`terraform apply`, then point the host at `public_ip` if you did not give a DNS zone. Caddy obtains the
certificate once the name resolves; the first boot takes a few minutes. The outputs:

| Output | What |
|---|---|
| `connector_url` | `https://<public host>/mcp`, for Claude or any MCP client |
| `public_ip` | The Elastic IP or static address |
| `instance_id` | The VM |
| `data_volume_id` (AWS), `data_disk` (Google Cloud) | The disk holding `/data`, to snapshot |

## Inputs

Both modules: `public_host`, `runtime_id`, `runtime_token` (required); `license_key` (written as `GLYPHH_LICENSE_KEY`; `offline_licence` is its old alias), `rrotor_version`
(default `latest`; pin one), `server`, `size`, `data_disk_gb`, `ingress_cidrs` (default anywhere: port 80 must stay
reachable by Let's Encrypt), and `name` for the resources. AWS adds `vpc_id`, `subnet_id`, `vpc_cidr`,
`instance_type`, `route53_zone_id` and `tags`; Google Cloud adds `project`, `region`, `zone`, `network`,
`subnetwork`, `subnetwork_cidr`, `machine_type`, `dns_managed_zone`, `allow_iap_ssh` and `labels`. A cluster
([below](#a-cluster)): `node_count` (default 1) and `cluster_secret` in both; AWS adds `subnet_ids` and
`certificate_arn`, Google Cloud `zones`. Each variable's description is in the module's `variables.tf`.

Logs, audit and drains ([README](README.md#logs-audit-and-drains)), in both modules: `otel_endpoint` (your
OpenTelemetry collector, `OTEL_EXPORTER_OTLP_ENDPOINT`; empty, no drain), `otel_headers` (sensitive,
`OTEL_EXPORTER_OTLP_HEADERS`), `otel_resource_attributes`, `server_logs` (default `false`: `RROTOR_SERVER_LOGS`),
`log_payloads` (default `false`), `audit_retention_days` (default `90`) and, for a cluster, `drain_at` (default
`head`: `RROTOR_DRAIN_AT`, the followers' signals exported by the head, so only it needs the egress; `node`, each VM
exports itself). Each is written to the VM's `.env`; any other setting can be added there.

## A cluster

`node_count` above 1 makes a cluster ([README, A cluster](README.md#a-cluster); a licence key with cluster sync,
`node_count` at most its `max_nodes`). See `examples/cluster/`.

| | AWS | Google Cloud |
|---|---|---|
| The nodes | `node_count` instances, `rrotor-0` the head, spread over up to three zones (a subnet in each, or your `subnet_ids`), each at a fixed private address (`.10`, `.11`, … of its subnet: keep them free in a subnet of yours), each with its own `/data` volume | The same, over `zones` (default the region's first three), each at a reserved internal address |
| On each | `docker-compose.node.yml` (rrotor alone, no Caddy) with its node id, its address and every peer in `.env` | The same |
| The secret | `cluster_secret` (sensitive; empty: made), in Secrets Manager; each node reads it at first boot with its role (the AWS CLI from snap), so it is not in the user data | In Secret Manager (the project needs its API on), read with the node's service account |
| Port 7778 | From the nodes' own security group only | From the nodes' network tag only |
| In front | A Network Load Balancer: TLS on 443 with an ACM certificate (`certificate_arn`, or made and validated in `route53_zone_id`), to 7777 on each node, health-checking `/readyz`; the nodes take 7777 from it only | Google's external HTTPS load balancer with a managed certificate, to 7777, health-checking `/readyz`; the nodes take 7777 from Google's ranges only. `ingress_cidrs` does not apply: use Cloud Armor |
| DNS | An alias to the load balancer in `route53_zone_id` | An A record to its address in `dns_managed_zone` |
| Outbound | Each node has a public address for going out, nothing in | The same, an ephemeral one |

Outputs added: `load_balancer_dns` (AWS), `instance_ids` or `instance_names`, `data_volume_ids` or `data_disks`, and
`cluster_secret_arn` or `cluster_secret_id`; `public_ip` is the load balancer's on Google Cloud and null on AWS.
`node_count` 1 is the single VM as before (state from an earlier version moves in place). Any change to the
first-boot configuration replaces every node at once; to roll it, apply one node at a time
(`-target='module.rrotor.aws_instance.rrotor[2]'`, the head, `[0]`, last). Growing `node_count` adds nodes but
changes every node's peers, so it replaces them too: grow a cluster when nothing is running.

## Where the token lives

The runtime token reaches the VM in its first-boot configuration, so it is in three places: the Terraform state
(marked sensitive; keep the state in an encrypted backend with access as tight as the token's), the instance's
user data or metadata (readable by anyone in the account or project allowed to read the instance's attributes, and
from inside the VM), and `/opt/rrotor/.env` on the VM (root only). Rotate the token on the platform if any of
these is exposed, then apply with the new one. A cluster's secret is in the state too, and in the cloud's secret
store, not in the user data.

## Upgrade, back up, remove

- **Upgrade:** set `rrotor_version` and apply. Any change to the first-boot configuration (the version, the size,
  the token) replaces the VM: the data disk is detached from the old one and attached to the new, which mounts it
  without formatting. A newer Ubuntu image alone does not replace it.
- **Back up:** snapshot the data disk (`data_volume_id`, `data_disk`): an AWS Backup or Data Lifecycle Manager
  policy, or a Compute Engine snapshot schedule ([README, Backups](README.md#backups)).
- **Remove:** `terraform destroy` deletes the VM **and the data disk**. Snapshot it first if you want `/data`.
  Delete the runtime on the platform when you are done with it.
