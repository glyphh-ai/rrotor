# A three-node rrotor cluster on Google Cloud (a licence key with cluster sync): its own network, the nodes across
# the region's zones, Google's external HTTPS load balancer with a managed certificate, and the DNS record in Cloud
# DNS. The project needs the Compute Engine and Secret Manager APIs on.
#   terraform init && terraform apply -var runtime_id=... -var runtime_token=...

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }
}

provider "google" {
  project = "acme-rrotor"
  region  = "europe-west2"
}

variable "runtime_id" {
  type = string
}

variable "runtime_token" {
  type      = string
  sensitive = true
}

module "rrotor" {
  # From outside this repository: "git::https://github.com/glyphh-ai/rrotor.git//deploy/terraform/gcp?ref=<release>"
  source = "../.."

  project          = "acme-rrotor"
  region           = "europe-west2"
  public_host      = "rrotor.acme.com"
  runtime_id       = var.runtime_id
  runtime_token    = var.runtime_token
  node_count       = 3
  size             = "medium"
  rrotor_version   = "latest"
  dns_managed_zone = "acme-com"
}

output "connector_url" {
  value = module.rrotor.connector_url
}

output "public_ip" {
  value = module.rrotor.public_ip
}

output "instance_names" {
  value = module.rrotor.instance_names
}
