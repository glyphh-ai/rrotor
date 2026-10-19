# rrotor on one EC2 instance: Docker Compose (deploy/compose) behind Caddy, its /data on a separate EBS volume that
# outlives the instance, reached over SSM rather than SSH (docs/self-hosting/terraform.md).

locals {
  # The instance has the size's memory and room for Ubuntu, Docker and Caddy besides.
  sizes = {
    small  = { instance_type = "t3.small", disk = 3, cpus = "1", memory = "1g" }
    medium = { instance_type = "t3.large", disk = 10, cpus = "2", memory = "4g" }
    large  = { instance_type = "r7i.large", disk = 20, cpus = "2", memory = "8g" }
  }
  size          = local.sizes[var.size]
  instance_type = coalesce(var.instance_type, local.size.instance_type)
  data_disk_gb  = coalesce(var.data_disk_gb, local.size.disk)
  tags          = merge({ "glyphh:rrotor" = var.name }, var.tags)

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
    # Nitro instances name an EBS volume by its id, without the dash.
    data_device = "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${replace(aws_ebs_volume.data.id, "-", "")}"
  })
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_instance" "rrotor" {
  ami                         = data.aws_ami.ubuntu.id
  instance_type               = local.instance_type
  subnet_id                   = local.subnet_id
  vpc_security_group_ids      = [aws_security_group.rrotor.id]
  iam_instance_profile        = aws_iam_instance_profile.rrotor.name
  user_data_base64            = base64gzip(local.user_data)
  user_data_replace_on_change = true # a new version or setting is a new instance; /data stays on its volume

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 20
    encrypted   = true
  }

  tags = merge(local.tags, { Name = var.name })

  lifecycle {
    ignore_changes = [ami] # a newer Ubuntu image does not replace a running rrotor
  }
}

resource "aws_ebs_volume" "data" {
  availability_zone = local.availability_zone
  size              = local.data_disk_gb
  type              = "gp3"
  encrypted         = true
  tags              = merge(local.tags, { Name = "${var.name}-data" })
}

resource "aws_volume_attachment" "data" {
  device_name                    = "/dev/sdf"
  volume_id                      = aws_ebs_volume.data.id
  instance_id                    = aws_instance.rrotor.id
  stop_instance_before_detaching = true
}

resource "aws_eip" "rrotor" {
  domain   = "vpc"
  instance = aws_instance.rrotor.id
  tags     = merge(local.tags, { Name = var.name })
}

resource "aws_route53_record" "rrotor" {
  count   = var.route53_zone_id == null ? 0 : 1
  zone_id = var.route53_zone_id
  name    = var.public_host
  type    = "A"
  ttl     = 300
  records = [aws_eip.rrotor.public_ip]
}
