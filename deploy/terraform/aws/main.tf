# rrotor on EC2: Docker Compose (deploy/compose), each instance's /data on a separate EBS volume that outlives it,
# reached over SSM rather than SSH (docs/self-hosting/terraform.md). One node (node_count 1): one instance behind
# Caddy, at an Elastic IP. A cluster: node_count instances across zones, each running docker-compose.node.yml at a
# fixed private address, behind a Network Load Balancer (cluster.tf).

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

  cluster = var.node_count > 1
  nodes = [for i in range(var.node_count) : {
    name   = local.cluster ? "${var.name}-${i}" : var.name
    id     = "rrotor-${i}"
    subnet = local.subnets[i % length(local.subnets)]
    # A cluster's nodes at fixed addresses (.10, .11, ... of their subnets), so each one's peers are known at boot.
    ip = local.cluster ? cidrhost(local.subnets[i % length(local.subnets)].cidr, 10 + floor(i / length(local.subnets))) : null
  }]
  peers = local.cluster ? join(",", [for n in local.nodes : "${n.ip}:7778"]) : ""

  user_data = [for i, n in local.nodes : templatefile("${path.module}/../shared/cloud-init.yaml.tftpl", {
    compose      = filebase64("${path.module}/../../compose/docker-compose.yml")
    caddyfile    = filebase64("${path.module}/../../compose/Caddyfile")
    node_compose = local.cluster ? filebase64("${path.module}/../../compose/docker-compose.node.yml") : ""
    # Read with the instance's role; the secret itself is never in the user data.
    secret_fetch = local.cluster ? join(" ", [
      "[ -x /snap/bin/aws ] || snap install aws-cli --classic >/dev/null;",
      "/snap/bin/aws secretsmanager get-secret-value --region ${split(":", aws_secretsmanager_secret.cluster[0].arn)[3]}",
      "--secret-id ${aws_secretsmanager_secret.cluster[0].arn} --query SecretString --output text",
    ]) : ""
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
      node_id       = local.cluster ? n.id : ""
      advertise     = local.cluster ? "${n.ip}:7778" : ""
      peers         = local.cluster ? local.peers : ""
      head          = local.cluster ? local.nodes[0].id : ""
    }))
    # Nitro instances name an EBS volume by its id, without the dash.
    data_device = "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${replace(aws_ebs_volume.data[i].id, "-", "")}"
  })]
}

# Before clusters, the one instance, its volume and its address were single resources.
moved {
  from = aws_instance.rrotor
  to   = aws_instance.rrotor[0]
}

moved {
  from = aws_ebs_volume.data
  to   = aws_ebs_volume.data[0]
}

moved {
  from = aws_volume_attachment.data
  to   = aws_volume_attachment.data[0]
}

moved {
  from = aws_eip.rrotor
  to   = aws_eip.rrotor[0]
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
  count                       = var.node_count
  ami                         = data.aws_ami.ubuntu.id
  instance_type               = local.instance_type
  subnet_id                   = local.nodes[count.index].subnet.id
  private_ip                  = local.nodes[count.index].ip
  associate_public_ip_address = local.cluster ? true : null # a cluster's nodes: outbound only, no Elastic IP
  vpc_security_group_ids      = [aws_security_group.rrotor.id]
  iam_instance_profile        = aws_iam_instance_profile.rrotor.name
  user_data_base64            = base64gzip(local.user_data[count.index])
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

  tags = merge(local.tags, { Name = local.nodes[count.index].name })

  # A cluster's node reads the secret at its first boot.
  depends_on = [aws_secretsmanager_secret_version.cluster, aws_iam_role_policy.cluster_secret]

  lifecycle {
    ignore_changes = [ami] # a newer Ubuntu image does not replace a running rrotor
    precondition {
      condition     = var.vpc_id == null || length(local.given_subnets) > 0
      error_message = "With vpc_id, give subnet_ids (or subnet_id): public subnets in that VPC."
    }
  }
}

resource "aws_ebs_volume" "data" {
  count             = var.node_count
  availability_zone = local.nodes[count.index].subnet.availability_zone
  size              = local.data_disk_gb
  type              = "gp3"
  encrypted         = true
  tags              = merge(local.tags, { Name = "${local.nodes[count.index].name}-data" })
}

resource "aws_volume_attachment" "data" {
  count                          = var.node_count
  device_name                    = "/dev/sdf"
  volume_id                      = aws_ebs_volume.data[count.index].id
  instance_id                    = aws_instance.rrotor[count.index].id
  stop_instance_before_detaching = true
}

resource "aws_eip" "rrotor" {
  count    = local.cluster ? 0 : 1
  domain   = "vpc"
  instance = aws_instance.rrotor[0].id
  tags     = merge(local.tags, { Name = var.name })
}

resource "aws_route53_record" "rrotor" {
  count   = var.route53_zone_id != null && !local.cluster ? 1 : 0
  zone_id = var.route53_zone_id
  name    = var.public_host
  type    = "A"
  ttl     = 300
  records = [aws_eip.rrotor[0].public_ip]
}
