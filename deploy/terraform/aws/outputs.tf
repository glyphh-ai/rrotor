output "connector_url" {
  description = "The Glyphh connector: add it in Claude or any MCP client."
  value       = "https://${var.public_host}/mcp"
}

output "public_ip" {
  description = "The Elastic IP: point public_host's A record here (done for you with route53_zone_id)."
  value       = aws_eip.rrotor.public_ip
}

output "instance_id" {
  description = "The instance: aws ssm start-session --target <id> for a shell."
  value       = aws_instance.rrotor.id
}

output "data_volume_id" {
  description = "The EBS volume holding /data: snapshot it to back rrotor up."
  value       = aws_ebs_volume.data.id
}
