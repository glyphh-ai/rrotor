output "connector_url" {
  description = "The Glyphh connector: add it in Claude or any MCP client."
  value       = "https://${var.public_host}/mcp"
}

output "public_ip" {
  description = "One node: the Elastic IP, where public_host's A record points (done for you with route53_zone_id). Null for a cluster: see load_balancer_dns."
  value       = one(aws_eip.rrotor[*].public_ip)
}

output "load_balancer_dns" {
  description = "A cluster: the load balancer's name, where public_host points (an alias, done for you with route53_zone_id). Null for one node."
  value       = one(aws_lb.rrotor[*].dns_name)
}

output "instance_id" {
  description = "The (first) instance: aws ssm start-session --target <id> for a shell."
  value       = aws_instance.rrotor[0].id
}

output "instance_ids" {
  description = "Every node's instance, in node order (rrotor-0, rrotor-1, ...)."
  value       = aws_instance.rrotor[*].id
}

output "data_volume_id" {
  description = "The (first) EBS volume holding /data: snapshot it to back rrotor up."
  value       = aws_ebs_volume.data[0].id
}

output "data_volume_ids" {
  description = "Every node's /data volume, in node order."
  value       = aws_ebs_volume.data[*].id
}

output "cluster_secret_arn" {
  description = "A cluster: the Secrets Manager secret holding the cluster secret. Null for one node."
  value       = one(aws_secretsmanager_secret.cluster[*].arn)
}
