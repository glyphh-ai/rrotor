# rrotor on AWS, in its own VPC, with its DNS record in Route 53.
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
  size            = "small"
  rrotor_version  = "latest"
  route53_zone_id = "Z0123456789EXAMPLE"
}

output "connector_url" {
  value = module.rrotor.connector_url
}

output "public_ip" {
  value = module.rrotor.public_ip
}

output "instance_id" {
  value = module.rrotor.instance_id
}
