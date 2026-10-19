# A three-node rrotor cluster on AWS (a licence key with cluster sync): its own VPC across three zones, a Network
# Load Balancer with an ACM certificate validated in Route 53, and the DNS alias there.
#   terraform init && terraform apply -var runtime_id=... -var runtime_token=...

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = "eu-west-2"
}

variable "runtime_id" {
  type = string
}

variable "runtime_token" {
  type      = string
  sensitive = true
}

module "rrotor" {
  # From outside this repository: "git::https://github.com/glyphh-ai/rrotor.git//deploy/terraform/aws?ref=<release>"
  source = "../.."

  public_host     = "rrotor.acme.com"
  runtime_id      = var.runtime_id
  runtime_token   = var.runtime_token
  node_count      = 3
  size            = "medium"
  rrotor_version  = "latest"
  route53_zone_id = "Z0123456789EXAMPLE"
}

output "connector_url" {
  value = module.rrotor.connector_url
}

output "load_balancer_dns" {
  value = module.rrotor.load_balancer_dns
}

output "instance_ids" {
  value = module.rrotor.instance_ids
}
