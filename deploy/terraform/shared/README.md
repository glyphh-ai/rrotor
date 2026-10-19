# Shared by the Terraform modules

The first-boot configuration both the AWS and the GCP module render: `cloud-init.yaml.tftpl` (Docker, the kernel
setting, the data disk, the Compose package from `deploy/compose`) and `env.tftpl` (its `.env`). One node runs
`docker-compose.yml` with Caddy; a cluster's node runs `docker-compose.node.yml` alone, with its node id, address
and peers in `.env`, and reads the cluster secret from the cloud's secret store at boot (`secret_fetch`, with the
instance's own identity), so the secret is never in the user data. A single node's rendering is unchanged by the
cluster's, so upgrading the module does not replace it. The modules read
these and `deploy/compose` by relative path, so use them from this repository (a `git::` source), not copied alone.
