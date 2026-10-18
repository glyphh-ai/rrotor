output "connector_url" {
  description = "The Glyphh connector: add it in Claude or any MCP client."
  value       = "https://${var.public_host}/mcp"
}

output "public_ip" {
  description = "The static address: point public_host's A record here (done for you with dns_managed_zone)."
  value       = google_compute_address.rrotor.address
}

output "instance_id" {
  description = "The instance. With allow_iap_ssh, gcloud compute ssh <name> --tunnel-through-iap is a shell."
  value       = google_compute_instance.rrotor.instance_id
}

output "instance_name" {
  description = "The instance's name."
  value       = google_compute_instance.rrotor.name
}

output "data_disk" {
  description = "The persistent disk holding /data: snapshot it to back rrotor up."
  value       = google_compute_disk.data.name
}
