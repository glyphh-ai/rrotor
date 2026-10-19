# rrotor with Terraform

One VM on AWS or Google Cloud, set up on its first boot to run the Docker Compose package
([compose.md](compose.md)) with `/data` on a disk of its own. The modules are `deploy/terraform/aws/` and
`deploy/terraform/gcp/`, each with a usage example in `examples/basic/`.

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
`subnetwork`, `subnetwork_cidr`, `machine_type`, `dns_managed_zone`, `allow_iap_ssh` and `labels`. Each variable's
description is in the module's `variables.tf`.

## Where the token lives

The runtime token reaches the VM in its first-boot configuration, so it is in three places: the Terraform state
(marked sensitive; keep the state in an encrypted backend with access as tight as the token's), the instance's
user data or metadata (readable by anyone in the account or project allowed to read the instance's attributes, and
from inside the VM), and `/opt/rrotor/.env` on the VM (root only). Rotate the token on the platform if any of
these is exposed, then apply with the new one.

## Upgrade, back up, remove

- **Upgrade:** set `rrotor_version` and apply. Any change to the first-boot configuration (the version, the size,
  the token) replaces the VM: the data disk is detached from the old one and attached to the new, which mounts it
  without formatting. A newer Ubuntu image alone does not replace it.
- **Back up:** snapshot the data disk (`data_volume_id`, `data_disk`): an AWS Backup or Data Lifecycle Manager
  policy, or a Compute Engine snapshot schedule ([README, Backups](README.md#backups)).
- **Remove:** `terraform destroy` deletes the VM **and the data disk**. Snapshot it first if you want `/data`.
  Delete the runtime on the platform when you are done with it.
