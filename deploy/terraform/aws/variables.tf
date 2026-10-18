variable "name" {
  description = "A name for this rrotor's resources."
  type        = string
  default     = "rrotor"
}

variable "public_host" {
  description = "The exact public host rrotor answers at, e.g. rrotor.acme.com."
  type        = string
}

variable "runtime_id" {
  description = "The runtime's id, from registering it on the platform."
  type        = string
}

variable "runtime_token" {
  description = "The runtime's token, from registering it on the platform (shown once)."
  type        = string
  sensitive   = true
}

variable "offline_licence" {
  description = "An offline licence token, optional: rrotor then runs past a lost link on it alone."
  type        = string
  default     = ""
  sensitive   = true
}

variable "rrotor_version" {
  description = "The image version, ghcr.io/glyphh-ai/rrotor:<version>. Pin a release in production."
  type        = string
  default     = "latest"
}

variable "server" {
  description = "The Glyphh server."
  type        = string
  default     = "https://api.glyphh.ai"
}

variable "size" {
  description = "small (1 CPU, 1 GiB, 3 GiB disk), medium (2, 4 GiB, 10 GiB) or large (2, 8 GiB, 20 GiB)."
  type        = string
  default     = "small"
  validation {
    condition     = contains(["small", "medium", "large"], var.size)
    error_message = "size must be small, medium or large."
  }
}

variable "instance_type" {
  description = "Overrides the size's instance type."
  type        = string
  default     = null
}

variable "data_disk_gb" {
  description = "Overrides the size's data disk, in GiB."
  type        = number
  default     = null
}

variable "vpc_id" {
  description = "An existing VPC, with subnet_id. Null makes a VPC with one public subnet."
  type        = string
  default     = null
}

variable "subnet_id" {
  description = "An existing public subnet in vpc_id (a route to an internet gateway)."
  type        = string
  default     = null
}

variable "vpc_cidr" {
  description = "The CIDR of the VPC made when vpc_id is null."
  type        = string
  default     = "10.80.0.0/16"
}

variable "ingress_cidrs" {
  description = "Who may reach ports 80 and 443. Port 80 must stay open to Let's Encrypt for the certificate."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "route53_zone_id" {
  description = "A Route 53 hosted zone to write public_host's A record in. Null leaves DNS to you."
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags for every resource."
  type        = map(string)
  default     = {}
}
