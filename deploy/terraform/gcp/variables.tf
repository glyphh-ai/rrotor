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
  description = "The zone, for one node's instance and its data disk."
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

variable "otel_endpoint" {
  description = "Your OpenTelemetry collector, OTLP over HTTP with protobuf (OTEL_EXPORTER_OTLP_ENDPOINT), e.g. http://otel-collector:4318. Empty: no drain; logs, metrics, traces and audit events stay on the VM."
  type        = string
  default     = ""

  validation {
    condition     = var.otel_endpoint == "" || can(regex("^https?://[^/\\s]+(/\\S*)?$", var.otel_endpoint))
    error_message = "otel_endpoint is the collector's http(s) URL."
  }
}

variable "otel_headers" {
  description = "OTEL_EXPORTER_OTLP_HEADERS (key=value,...), e.g. your backend's API key."
  type        = string
  default     = ""
  sensitive   = true
}

variable "otel_resource_attributes" {
  description = "OTEL_RESOURCE_ATTRIBUTES (key=value,...) added to every signal."
  type        = string
  default     = ""
}

variable "server_logs" {
  description = "Also send the logs, metrics, traces and audit events to Glyphh (RROTOR_SERVER_LOGS=on), so the platform can show them. They then leave your infrastructure (never inputs, results or callers' addresses)."
  type        = bool
  default     = false
}

variable "drain_at" {
  description = "In a cluster (RROTOR_DRAIN_AT): head, the followers' logs, metrics, traces and audit events go by the head, which exports for every node (one egress); node, each node exports itself."
  type        = string
  default     = "head"

  validation {
    condition     = contains(["head", "node"], var.drain_at)
    error_message = "drain_at is head or node."
  }
}

variable "log_payloads" {
  description = "Models' inputs and results in your drain too, 4 KB each (RROTOR_LOG_PAYLOADS=on); never anywhere else."
  type        = bool
  default     = false
}

variable "audit_retention_days" {
  description = "Enterprise: days of audit log kept on the data disk (RROTOR_AUDIT_RETENTION_DAYS)."
  type        = number
  default     = 90

  validation {
    condition     = var.audit_retention_days >= 1 && floor(var.audit_retention_days) == var.audit_retention_days
    error_message = "audit_retention_days is a whole number of days, 1 or more."
  }
}

variable "node_count" {
  description = "The nodes: 1 is one VM behind Caddy; more is a cluster (a licence key with cluster sync, at most its max_nodes) across zones behind Google's external HTTPS load balancer."
  type        = number
  default     = 1

  validation {
    condition     = var.node_count >= 1 && floor(var.node_count) == var.node_count
    error_message = "node_count is a whole number, 1 or more."
  }
}

variable "zones" {
  description = "A cluster: the zones its nodes are spread over, in order. Empty: the region's first three. One node uses zone."
  type        = list(string)
  default     = []
}

variable "cluster_secret" {
  description = "A cluster's secret (RROTOR_CLUSTER_SECRET): 32 or more random bytes, base64 (openssl rand -base64 32). Empty: one is made. Kept in Secret Manager; each node reads it at boot."
  type        = string
  default     = ""
  sensitive   = true
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
  description = "Who may reach ports 80 and 443 on one node (a cluster's load balancer is open to all: limit it with Cloud Armor). Port 80 must stay open to Let's Encrypt for the certificate."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "dns_managed_zone" {
  description = "A Cloud DNS managed zone (its name) to write public_host's A record in (a cluster: the load balancer's address). Null leaves DNS to you."
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
