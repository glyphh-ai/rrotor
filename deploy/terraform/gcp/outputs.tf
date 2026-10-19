output "connector_url" {
  description = "The Glyphh connector: add it in Claude or any MCP client."
  value       = "https://${var.public_host}/mcp"
}

output "public_ip" {
  description = "Where public_host's A record points (done for you with dns_managed_zone): one node's static address, or a cluster's load balancer."
  value       = local.cluster ? google_compute_global_address.lb[0].address : google_compute_address.rrotor[0].address
}

output "instance_id" {
  description = "The (first) instance. With allow_iap_ssh, gcloud compute ssh <name> --tunnel-through-iap is a shell."
  value       = google_compute_instance.rrotor[0].instance_id
}

output "instance_name" {
  description = "The (first) instance's name."
  value       = google_compute_instance.rrotor[0].name
}

output "instance_names" {
  description = "Every node's instance, in node order (rrotor-0, rrotor-1, ...)."
  value       = google_compute_instance.rrotor[*].name
}

output "data_disk" {
  description = "The (first) persistent disk holding /data: snapshot it to back rrotor up."
  value       = google_compute_disk.data[0].name
}

output "data_disks" {
  description = "Every node's /data disk, in node order."
  value       = google_compute_disk.data[*].name
}

output "cluster_secret_id" {
  description = "A cluster: the Secret Manager secret holding the cluster secret. Null for one node."
  value       = one(google_secret_manager_secret.cluster[*].id)
}
