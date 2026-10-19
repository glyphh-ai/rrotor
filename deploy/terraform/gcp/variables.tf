variable "name" {
  description = "A name for this rrotor's resources (lower case, letters, digits and dashes)."
  type        = string
  default     = "rrotor"
}

variable "project" {
  description = "The Google Cloud project."
  type        = string
}

variable "region" {
  description = "The region, for the static address and a subnetwork made here."
  type        = string
  default     = "europe-west2"
}

variable "zone" {
  description = "The zone, for the instance and its data disk."
  type        = string
  default     = "europe-west2-a"
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

variable "license_key" {
  description = "The licence key, optional: written as GLYPHH_LICENSE_KEY. Normally it arrives over the link or the heartbeat; set it for an air-gapped rrotor (without one, rrotor runs at Community's limits)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "offline_licence" {
  description = "Deprecated alias of license_key (an earlier release's offline licence token), used when license_key is empty."
  type        = string
  default     = ""
  sensitive   = true
}

variable "runtime_keys_sha256" {
  description = "Air-gapped: the SHA-256 of each runtime key that may call it. With the link on, the keys come down the link."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for k in var.runtime_keys_sha256 : can(regex("^[0-9a-f]{64}$", k))])
    error_message = "Each runtime_keys_sha256 entry is 64 lowercase hex digits."
  }
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

variable "machine_type" {
  description = "Overrides the size's machine type."
  type        = string
  default     = null
}

variable "data_disk_gb" {
  description = "Overrides the size's data disk, in GiB."
  type        = number
  default     = null
}

variable "network" {
  description = "An existing VPC network (name or self link), with subnetwork. Null makes one network and subnetwork."
  type        = string
  default     = null
}

variable "subnetwork" {
  description = "An existing subnetwork in network, in region."
  type        = string
  default     = null
}

variable "subnetwork_cidr" {
  description = "The CIDR of the subnetwork made when network is null."
  type        = string
  default     = "10.80.0.0/24"
}

variable "ingress_cidrs" {
  description = "Who may reach ports 80 and 443. Port 80 must stay open to Let's Encrypt for the certificate."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "dns_managed_zone" {
  description = "A Cloud DNS managed zone (its name) to write public_host's A record in. Null leaves DNS to you."
  type        = string
  default     = null
}

variable "labels" {
  description = "Labels for every resource that takes them."
  type        = map(string)
  default     = {}
}

variable "allow_iap_ssh" {
  description = "Allow SSH from Identity-Aware Proxy only (OS Login, no keys), for gcloud compute ssh --tunnel-through-iap."
  type        = bool
  default     = false
}
