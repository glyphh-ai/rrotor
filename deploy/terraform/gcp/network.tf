# The network: an existing network and subnetwork, or one of each made here.

locals {
  make_network = var.network == null
  network      = local.make_network ? google_compute_network.rrotor[0].self_link : var.network
  subnetwork   = local.make_network ? google_compute_subnetwork.rrotor[0].self_link : var.subnetwork
}

resource "google_compute_network" "rrotor" {
  count                   = local.make_network ? 1 : 0
  project                 = var.project
  name                    = var.name
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "rrotor" {
  count         = local.make_network ? 1 : 0
  project       = var.project
  name          = var.name
  region        = var.region
  network       = google_compute_network.rrotor[0].self_link
  ip_cidr_range = var.subnetwork_cidr
}

# One node: HTTPS in (and HTTP, for the certificate and the redirect). A cluster: the API from Google's load
# balancer and the cluster's port from the other nodes only (cluster.tf). Egress is open by Compute Engine's default
# rule: the link and the heartbeat to the Glyphh server, the image pull, Let's Encrypt, and the egress models.
resource "google_compute_firewall" "web" {
  count         = local.cluster ? 0 : 1
  project       = var.project
  name          = "${var.name}-web"
  network       = local.network
  direction     = "INGRESS"
  source_ranges = var.ingress_cidrs
  target_tags   = ["${var.name}-web"]

  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }

  allow {
    protocol = "udp"
    ports    = ["443"]
  }
}

# A shell, when asked for: SSH from Identity-Aware Proxy's range only, signed in through OS Login.
resource "google_compute_firewall" "iap_ssh" {
  count         = var.allow_iap_ssh ? 1 : 0
  project       = var.project
  name          = "${var.name}-iap-ssh"
  network       = local.network
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]
  target_tags   = [local.tag]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

moved {
  from = google_compute_firewall.web
  to   = google_compute_firewall.web[0]
}
