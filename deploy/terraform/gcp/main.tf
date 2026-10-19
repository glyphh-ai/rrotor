# rrotor on Compute Engine: Docker Compose (deploy/compose), each instance's /data on a separate persistent disk
# that outlives it, no SSH keys (docs/self-hosting/terraform.md). One node (node_count 1): one instance behind Caddy,
# at a static address. A cluster: node_count instances across zones, each running docker-compose.node.yml at a
# reserved internal address, behind Google's external HTTPS load balancer (cluster.tf).

locals {
  # The instance has the size's memory and room for Ubuntu, Docker and Caddy besides.
  sizes = {
    small  = { machine_type = "e2-small", disk = 3, cpus = "1", memory = "1g" }
    medium = { machine_type = "e2-standard-2", disk = 10, cpus = "2", memory = "4g" }
    large  = { machine_type = "e2-highmem-2", disk = 20, cpus = "2", memory = "8g" }
  }
  size         = local.sizes[var.size]
  machine_type = coalesce(var.machine_type, local.size.machine_type)
  # Compute Engine's smallest balanced persistent disk is 10 GB.
  data_disk_gb = max(10, coalesce(var.data_disk_gb, local.size.disk))
  labels       = merge({ "glyphh-rrotor" = var.name }, var.labels)

  cluster = var.node_count > 1
  zones = length(var.zones) > 0 ? var.zones : (
    local.cluster ? slice(data.google_compute_zones.available[0].names, 0, min(3, length(data.google_compute_zones.available[0].names))) : [var.zone]
  )
  tag = local.cluster ? "${var.name}-node" : "${var.name}-web"
  nodes = [for i in range(var.node_count) : {
    name = local.cluster ? "${var.name}-${i}" : var.name
    id   = "rrotor-${i}"
    zone = local.zones[i % length(local.zones)]
  }]
  peers = join(",", [for a in google_compute_address.internal : "${a.address}:7778"])

  # Read with the instance's service account (a token from the metadata server); never in the user data.
  secret_fetch = local.cluster ? trimspace(<<-EOT
    t=$(curl -sf -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])') && curl -sf -H "Authorization: Bearer $t" https://secretmanager.googleapis.com/v1/projects/${var.project}/secrets/${google_secret_manager_secret.cluster[0].secret_id}/versions/latest:access | python3 -c 'import base64,json,sys; print(base64.b64decode(json.load(sys.stdin)["payload"]["data"]).decode())'
  EOT
  ) : ""

  user_data = [for i, n in local.nodes : templatefile("${path.module}/../shared/cloud-init.yaml.tftpl", {
    compose      = filebase64("${path.module}/../../compose/docker-compose.yml")
    caddyfile    = filebase64("${path.module}/../../compose/Caddyfile")
    node_compose = local.cluster ? filebase64("${path.module}/../../compose/docker-compose.node.yml") : ""
    secret_fetch = local.secret_fetch
    env = base64encode(templatefile("${path.module}/../shared/env.tftpl", {
      runtime_id    = var.runtime_id
      runtime_token = var.runtime_token
      public_host   = var.public_host
      version       = var.rrotor_version
      server        = var.server
      cpus          = local.size.cpus
      memory        = local.size.memory
      licence       = var.license_key != "" ? var.license_key : var.offline_licence
      keys          = join(",", var.runtime_keys_sha256)
      otel_endpoint = var.otel_endpoint
      otel_headers  = var.otel_headers
      otel_resource = var.otel_resource_attributes
      server_logs   = var.server_logs ? "on" : "off"
      log_payloads  = var.log_payloads ? "on" : "off"
      drain_at      = var.drain_at
      audit_days    = var.audit_retention_days
      node_id       = local.cluster ? n.id : ""
      advertise     = local.cluster ? "${google_compute_address.internal[i].address}:7778" : ""
      peers         = local.cluster ? local.peers : ""
      head          = local.cluster ? local.nodes[0].id : ""
    }))
    data_device = "/dev/disk/by-id/google-rrotor-data"
  })]
}

data "google_compute_zones" "available" {
  count   = local.cluster && length(var.zones) == 0 ? 1 : 0
  project = var.project
  region  = var.region
}

# Before clusters, the one instance, its disk and its address were single resources.
moved {
  from = google_compute_instance.rrotor
  to   = google_compute_instance.rrotor[0]
}

moved {
  from = google_compute_disk.data
  to   = google_compute_disk.data[0]
}

moved {
  from = terraform_data.user_data
  to   = terraform_data.user_data[0]
}

moved {
  from = google_compute_address.rrotor
  to   = google_compute_address.rrotor[0]
}

data "google_compute_image" "ubuntu" {
  project = "ubuntu-os-cloud"
  family  = "ubuntu-2404-lts-amd64"
}

resource "google_compute_disk" "data" {
  count   = var.node_count
  project = var.project
  name    = "${local.nodes[count.index].name}-data"
  zone    = local.nodes[count.index].zone
  type    = "pd-balanced"
  size    = local.data_disk_gb
  labels  = local.labels
}

# A new version or setting is a new instance (cloud-init runs on an instance's first boot); /data stays on its disk.
resource "terraform_data" "user_data" {
  count = var.node_count
  input = sha256(local.user_data[count.index])
}

# A cluster's nodes at reserved internal addresses, so each one's peers are known at boot.
resource "google_compute_address" "internal" {
  count        = local.cluster ? var.node_count : 0
  project      = var.project
  name         = "${local.nodes[count.index].name}-internal"
  region       = var.region
  subnetwork   = local.subnetwork
  address_type = "INTERNAL"
}

resource "google_compute_instance" "rrotor" {
  count        = var.node_count
  project      = var.project
  name         = local.nodes[count.index].name
  zone         = local.nodes[count.index].zone
  machine_type = local.machine_type
  tags         = [local.tag]
  labels       = local.labels

  boot_disk {
    initialize_params {
      image = data.google_compute_image.ubuntu.self_link
      size  = 20
      type  = "pd-balanced"
    }
  }

  attached_disk {
    source      = google_compute_disk.data[count.index].id
    device_name = "rrotor-data"
  }

  network_interface {
    subnetwork = local.subnetwork
    network_ip = local.cluster ? google_compute_address.internal[count.index].address : null
    # One node: its static address. A cluster's node: an ephemeral one, for going out only.
    access_config {
      nat_ip = local.cluster ? null : google_compute_address.rrotor[0].address
    }
  }

  metadata = {
    user-data              = local.user_data[count.index]
    block-project-ssh-keys = "true"
    enable-oslogin         = "TRUE"
  }

  service_account {
    email  = google_service_account.rrotor.email
    scopes = ["cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  allow_stopping_for_update = true

  # A cluster's node reads the secret at its first boot.
  depends_on = [google_secret_manager_secret_version.cluster, google_secret_manager_secret_iam_member.cluster]

  lifecycle {
    ignore_changes       = [boot_disk[0].initialize_params[0].image] # a newer Ubuntu image does not replace rrotor
    replace_triggered_by = [terraform_data.user_data[count.index]]
  }
}

resource "google_compute_address" "rrotor" {
  count   = local.cluster ? 0 : 1
  project = var.project
  name    = var.name
  region  = var.region
}

resource "google_dns_record_set" "rrotor" {
  count        = var.dns_managed_zone == null ? 0 : 1
  project      = var.project
  managed_zone = var.dns_managed_zone
  name         = "${var.public_host}."
  type         = "A"
  ttl          = 300
  rrdatas      = [local.cluster ? google_compute_global_address.lb[0].address : google_compute_address.rrotor[0].address]
}
