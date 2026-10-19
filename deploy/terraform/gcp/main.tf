# rrotor on one Compute Engine instance: Docker Compose (deploy/compose) behind Caddy, its /data on a separate
# persistent disk that outlives the instance, no SSH keys (docs/self-hosting/terraform.md).

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

  user_data = templatefile("${path.module}/../shared/cloud-init.yaml.tftpl", {
    compose   = filebase64("${path.module}/../../compose/docker-compose.yml")
    caddyfile = filebase64("${path.module}/../../compose/Caddyfile")
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
      audit_days    = var.audit_retention_days
    }))
    data_device = "/dev/disk/by-id/google-rrotor-data"
  })
}

data "google_compute_image" "ubuntu" {
  project = "ubuntu-os-cloud"
  family  = "ubuntu-2404-lts-amd64"
}

resource "google_compute_disk" "data" {
  project = var.project
  name    = "${var.name}-data"
  zone    = var.zone
  type    = "pd-balanced"
  size    = local.data_disk_gb
  labels  = local.labels
}

# A new version or setting is a new instance (cloud-init runs on an instance's first boot); /data stays on its disk.
resource "terraform_data" "user_data" {
  input = sha256(local.user_data)
}

resource "google_compute_instance" "rrotor" {
  project      = var.project
  name         = var.name
  zone         = var.zone
  machine_type = local.machine_type
  tags         = ["${var.name}-web"]
  labels       = local.labels

  boot_disk {
    initialize_params {
      image = data.google_compute_image.ubuntu.self_link
      size  = 20
      type  = "pd-balanced"
    }
  }

  attached_disk {
    source      = google_compute_disk.data.id
    device_name = "rrotor-data"
  }

  network_interface {
    subnetwork = local.subnetwork
    access_config {
      nat_ip = google_compute_address.rrotor.address
    }
  }

  metadata = {
    user-data              = local.user_data
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

  lifecycle {
    ignore_changes       = [boot_disk[0].initialize_params[0].image] # a newer Ubuntu image does not replace rrotor
    replace_triggered_by = [terraform_data.user_data]
  }
}

resource "google_compute_address" "rrotor" {
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
  rrdatas      = [google_compute_address.rrotor.address]
}
