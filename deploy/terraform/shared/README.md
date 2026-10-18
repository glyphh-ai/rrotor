# Shared by the Terraform modules

The first-boot configuration both the AWS and the GCP module render: `cloud-init.yaml.tftpl` (Docker, the kernel
setting, the data disk, the Compose package from `deploy/compose`) and `env.tftpl` (its `.env`). The modules read
these and `deploy/compose` by relative path, so use them from this repository (a `git::` source), not copied alone.
