# A cluster (node_count above 1): the cluster secret in Secrets Manager, read by each node with its role at boot;
# port 7778 between the nodes only; a Network Load Balancer ending TLS for public_host (an ACM certificate) and
# sending the API to every node whose /readyz answers 200 (docs/self-hosting/terraform.md, "A cluster").

resource "random_bytes" "cluster_secret" {
  count  = local.cluster && var.cluster_secret == "" ? 1 : 0
  length = 32
}

locals {
  cluster_secret = var.cluster_secret != "" ? var.cluster_secret : one(random_bytes.cluster_secret[*].base64)
}

resource "aws_secretsmanager_secret" "cluster" {
  count       = local.cluster ? 1 : 0
  name_prefix = "${var.name}-cluster-"
  description = "rrotor ${var.name}: the cluster secret (RROTOR_CLUSTER_SECRET)"
  tags        = local.tags
}

resource "aws_secretsmanager_secret_version" "cluster" {
  count         = local.cluster ? 1 : 0
  secret_id     = aws_secretsmanager_secret.cluster[0].id
  secret_string = local.cluster_secret
}

data "aws_iam_policy_document" "cluster_secret" {
  count = local.cluster ? 1 : 0
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.cluster[0].arn]
  }
}

resource "aws_iam_role_policy" "cluster_secret" {
  count  = local.cluster ? 1 : 0
  name   = "cluster-secret"
  role   = aws_iam_role.rrotor.id
  policy = data.aws_iam_policy_document.cluster_secret[0].json
}

# The cluster's port, from the other nodes only; the API, from the load balancer only.
resource "aws_vpc_security_group_ingress_rule" "cluster" {
  count                        = local.cluster ? 1 : 0
  security_group_id            = aws_security_group.rrotor.id
  referenced_security_group_id = aws_security_group.rrotor.id
  ip_protocol                  = "tcp"
  from_port                    = 7778
  to_port                      = 7778
}

resource "aws_vpc_security_group_ingress_rule" "api" {
  count                        = local.cluster ? 1 : 0
  security_group_id            = aws_security_group.rrotor.id
  referenced_security_group_id = aws_security_group.lb[0].id
  ip_protocol                  = "tcp"
  from_port                    = 7777
  to_port                      = 7777
}

resource "aws_security_group" "lb" {
  count       = local.cluster ? 1 : 0
  name_prefix = "${var.name}-lb-"
  description = "rrotor load balancer: HTTPS in, the API to the nodes"
  vpc_id      = local.vpc_id
  tags        = merge(local.tags, { Name = "${var.name}-lb" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "lb_https" {
  for_each          = toset(local.cluster ? var.ingress_cidrs : [])
  security_group_id = aws_security_group.lb[0].id
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "lb_api" {
  count                        = local.cluster ? 1 : 0
  security_group_id            = aws_security_group.lb[0].id
  referenced_security_group_id = aws_security_group.rrotor.id
  ip_protocol                  = "tcp"
  from_port                    = 7777
  to_port                      = 7777
}

# The certificate: certificate_arn, or one made here and validated in route53_zone_id.
resource "aws_acm_certificate" "rrotor" {
  count             = local.cluster && var.certificate_arn == null ? 1 : 0
  domain_name       = var.public_host
  validation_method = "DNS"
  tags              = local.tags

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "validation" {
  count           = local.cluster && var.certificate_arn == null && var.route53_zone_id != null ? 1 : 0
  zone_id         = var.route53_zone_id
  name            = one(aws_acm_certificate.rrotor[0].domain_validation_options).resource_record_name
  type            = one(aws_acm_certificate.rrotor[0].domain_validation_options).resource_record_type
  ttl             = 300
  records         = [one(aws_acm_certificate.rrotor[0].domain_validation_options).resource_record_value]
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "rrotor" {
  count                   = length(aws_acm_certificate.rrotor)
  certificate_arn         = aws_acm_certificate.rrotor[0].arn
  validation_record_fqdns = aws_route53_record.validation[*].fqdn
}

resource "aws_lb" "rrotor" {
  count              = local.cluster ? 1 : 0
  name_prefix        = "rrotor"
  load_balancer_type = "network"
  subnets            = distinct([for n in local.nodes : n.subnet.id])
  security_groups    = [aws_security_group.lb[0].id]
  tags               = merge(local.tags, { Name = var.name })

  lifecycle {
    precondition {
      condition     = var.certificate_arn != null || var.route53_zone_id != null
      error_message = "A cluster's load balancer needs a certificate: certificate_arn, or route53_zone_id to make and validate one."
    }
  }
}

resource "aws_lb_target_group" "api" {
  count                = local.cluster ? 1 : 0
  name_prefix          = "rrotor"
  port                 = 7777
  protocol             = "TCP"
  vpc_id               = local.vpc_id
  deregistration_delay = 60
  tags                 = local.tags

  # Only the nodes that may take traffic: /readyz is 503 while a node joins, catches up or is refused.
  health_check {
    protocol            = "HTTP"
    path                = "/readyz"
    port                = "traffic-port"
    matcher             = "200"
    interval            = 10
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_lb_target_group_attachment" "api" {
  count            = local.cluster ? var.node_count : 0
  target_group_arn = aws_lb_target_group.api[0].arn
  target_id        = aws_instance.rrotor[count.index].id
  port             = 7777
}

resource "aws_lb_listener" "https" {
  count             = local.cluster ? 1 : 0
  load_balancer_arn = aws_lb.rrotor[0].arn
  port              = 443
  protocol          = "TLS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn != null ? var.certificate_arn : aws_acm_certificate_validation.rrotor[0].certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api[0].arn
  }
}

resource "aws_route53_record" "lb" {
  count   = local.cluster && var.route53_zone_id != null ? 1 : 0
  zone_id = var.route53_zone_id
  name    = var.public_host
  type    = "A"

  alias {
    name                   = aws_lb.rrotor[0].dns_name
    zone_id                = aws_lb.rrotor[0].zone_id
    evaluate_target_health = true
  }
}
