# A cluster (node_count above 1): the cluster secret in Secret Manager, read by each node with its service account
# at boot; port 7778 between the nodes only; Google's external HTTPS load balancer with a managed certificate for
# public_host, sending the API to every node whose /readyz answers 200 (docs/self-hosting/terraform.md, "A cluster").

resource "random_bytes" "cluster_secret" {
  count  = local.cluster && var.cluster_secret == "" ? 1 : 0
  length = 32
}

resource "google_secret_manager_secret" "cluster" {
  count     = local.cluster ? 1 : 0
  project   = var.project
  secret_id = "${var.name}-cluster"
  labels    = local.labels

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "cluster" {
  count       = local.cluster ? 1 : 0
  secret      = google_secret_manager_secret.cluster[0].id
  secret_data = var.cluster_secret != "" ? var.cluster_secret : random_bytes.cluster_secret[0].base64
}

resource "google_secret_manager_secret_iam_member" "cluster" {
  count     = local.cluster ? 1 : 0
  project   = var.project
  secret_id = google_secret_manager_secret.cluster[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.rrotor.email}"
}

# The cluster's port, from the other nodes only.
resource "google_compute_firewall" "cluster" {
  count       = local.cluster ? 1 : 0
  project     = var.project
  name        = "${var.name}-cluster"
  network     = local.network
  direction   = "INGRESS"
  source_tags = [local.tag]
  target_tags = [local.tag]

  allow {
    protocol = "tcp"
    ports    = ["7778"]
  }
}

# The API, from Google's load balancers and health checks only.
resource "google_compute_firewall" "lb" {
  count         = local.cluster ? 1 : 0
  project       = var.project
  name          = "${var.name}-lb"
  network       = local.network
  direction     = "INGRESS"
  source_ranges = ["35.191.0.0/16", "130.211.0.0/22"]
  target_tags   = [local.tag]

  allow {
    protocol = "tcp"
    ports    = ["7777"]
  }
}

# The nodes of each zone, as a backend.
resource "google_compute_instance_group" "nodes" {
  count     = local.cluster ? length(local.zones) : 0
  project   = var.project
  name      = "${var.name}-${local.zones[count.index]}"
  zone      = local.zones[count.index]
  instances = [for i, n in local.nodes : google_compute_instance.rrotor[i].self_link if n.zone == local.zones[count.index]]

  named_port {
    name = "api"
    port = 7777
  }
}

# Only the nodes that may take traffic: /readyz is 503 while a node joins, catches up or is refused.
resource "google_compute_health_check" "readyz" {
  count               = local.cluster ? 1 : 0
  project             = var.project
  name                = "${var.name}-readyz"
  check_interval_sec  = 5
  timeout_sec         = 3
  healthy_threshold   = 2
  unhealthy_threshold = 2

  http_health_check {
    port         = 7777
    request_path = "/readyz"
  }
}

resource "google_compute_backend_service" "api" {
  count                 = local.cluster ? 1 : 0
  project               = var.project
  name                  = "${var.name}-api"
  protocol              = "HTTP"
  port_name             = "api"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  timeout_sec           = 3600 # long MCP streams
  health_checks         = [google_compute_health_check.readyz[0].id]

  dynamic "backend" {
    for_each = google_compute_instance_group.nodes
    content {
      group           = backend.value.id
      balancing_mode  = "UTILIZATION"
      capacity_scaler = 1
    }
  }
}

resource "google_compute_url_map" "lb" {
  count           = local.cluster ? 1 : 0
  project         = var.project
  name            = var.name
  default_service = google_compute_backend_service.api[0].id
}

resource "google_compute_managed_ssl_certificate" "lb" {
  count   = local.cluster ? 1 : 0
  project = var.project
  name    = "${var.name}-cert"

  managed {
    domains = [var.public_host]
  }
}

resource "google_compute_target_https_proxy" "lb" {
  count            = local.cluster ? 1 : 0
  project          = var.project
  name             = var.name
  url_map          = google_compute_url_map.lb[0].id
  ssl_certificates = [google_compute_managed_ssl_certificate.lb[0].id]
}

resource "google_compute_global_address" "lb" {
  count   = local.cluster ? 1 : 0
  project = var.project
  name    = "${var.name}-lb"
}

resource "google_compute_global_forwarding_rule" "https" {
  count                 = local.cluster ? 1 : 0
  project               = var.project
  name                  = "${var.name}-https"
  ip_address            = google_compute_global_address.lb[0].id
  ip_protocol           = "TCP"
  port_range            = "443"
  target                = google_compute_target_https_proxy.lb[0].id
  load_balancing_scheme = "EXTERNAL_MANAGED"
}
