# The network: an existing VPC and subnet, or one VPC with one public subnet made here.

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_subnet" "existing" {
  count = var.subnet_id == null ? 0 : 1
  id    = var.subnet_id
}

locals {
  make_vpc          = var.vpc_id == null
  vpc_id            = local.make_vpc ? aws_vpc.rrotor[0].id : var.vpc_id
  subnet_id         = local.make_vpc ? aws_subnet.rrotor[0].id : var.subnet_id
  availability_zone = local.make_vpc ? aws_subnet.rrotor[0].availability_zone : data.aws_subnet.existing[0].availability_zone
}

resource "aws_vpc" "rrotor" {
  count                = local.make_vpc ? 1 : 0
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(local.tags, { Name = var.name })
}

resource "aws_internet_gateway" "rrotor" {
  count  = local.make_vpc ? 1 : 0
  vpc_id = aws_vpc.rrotor[0].id
  tags   = merge(local.tags, { Name = var.name })
}

resource "aws_subnet" "rrotor" {
  count             = local.make_vpc ? 1 : 0
  vpc_id            = aws_vpc.rrotor[0].id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 0)
  availability_zone = data.aws_availability_zones.available.names[0]
  tags              = merge(local.tags, { Name = "${var.name}-public" })
}

resource "aws_route_table" "rrotor" {
  count  = local.make_vpc ? 1 : 0
  vpc_id = aws_vpc.rrotor[0].id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.rrotor[0].id
  }
  tags = merge(local.tags, { Name = var.name })
}

resource "aws_route_table_association" "rrotor" {
  count          = local.make_vpc ? 1 : 0
  subnet_id      = aws_subnet.rrotor[0].id
  route_table_id = aws_route_table.rrotor[0].id
}

# HTTPS in (and HTTP, for the certificate and the redirect), everything out: the link and the heartbeat to the
# Glyphh server, the image pull, Let's Encrypt, and the egress models are allowed. No SSH: SSM instead.
resource "aws_security_group" "rrotor" {
  name_prefix = "${var.name}-"
  description = "rrotor: HTTPS and HTTP in, all out"
  vpc_id      = local.vpc_id
  tags        = merge(local.tags, { Name = var.name })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  for_each          = toset(var.ingress_cidrs)
  security_group_id = aws_security_group.rrotor.id
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "http3" {
  for_each          = toset(var.ingress_cidrs)
  security_group_id = aws_security_group.rrotor.id
  cidr_ipv4         = each.value
  ip_protocol       = "udp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  for_each          = toset(var.ingress_cidrs)
  security_group_id = aws_security_group.rrotor.id
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.rrotor.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
