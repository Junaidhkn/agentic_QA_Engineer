# Edge: a public ALB (app/api/otlp hosts) behind WAF, and a private path for runners —
# NLB → internal ALB, exported as a PrivateLink endpoint service to the execution accounts.
# The runner lease API (/internal/runner/*) is only reachable over PrivateLink; /metrics is
# never routed by either load balancer.

variable "route53_zone_id" {
  type        = string
  default     = null
  description = "Public hosted zone for <domain>; records are created when set"
}

locals {
  hosts = {
    app     = "app.${var.domain}"
    api     = "api.${var.domain}"
    otlp    = "otlp.${var.domain}"
    runners = "runners.${var.domain}"
  }
}

resource "aws_security_group" "alb_public" {
  name        = "${var.name}-alb-public"
  description = "Public ALB: HTTPS (and HTTP redirect) from anywhere"
  vpc_id      = var.vpc_id
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }
  tags = var.tags
}

resource "aws_security_group" "alb_internal" {
  name        = "${var.name}-alb-internal"
  description = "Internal ALB for runners: HTTPS from the PrivateLink NLB (VPC addresses)"
  vpc_id      = var.vpc_id
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }
  tags = var.tags
}

resource "aws_security_group" "tasks" {
  name        = "${var.name}-tasks"
  description = "ECS tasks: service ports from the load balancers; metrics scrapes within the group"
  vpc_id      = var.vpc_id
  ingress {
    description     = "web, api, otlp gateway from the load balancers"
    from_port       = 3000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb_public.id, aws_security_group.alb_internal.id]
  }
  ingress {
    description = "metrics scrapes and service-to-service calls (collector, OTLP gateway)"
    from_port   = 3000
    to_port     = 9464
    protocol    = "tcp"
    self        = true
  }
  egress {
    description = "AWS APIs, GitHub, LLM provider, customer OTLP backends (via NAT)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = var.tags
}

# --- Public ALB --------------------------------------------------------------------------
resource "aws_lb" "public" {
  name                       = "${var.name}-public"
  load_balancer_type         = "application"
  internal                   = false
  subnets                    = var.public_subnet_ids
  security_groups            = [aws_security_group.alb_public.id]
  drop_invalid_header_fields = true
  idle_timeout               = 300 # SSE streams
  enable_deletion_protection = var.deletion_protection
  access_logs {
    bucket  = aws_s3_bucket.alb_logs.id
    prefix  = "alb/public"
    enabled = true
  }
  tags       = var.tags
  depends_on = [aws_s3_bucket_policy.all]
}

resource "aws_lb_target_group" "web" {
  name        = "${var.name}-web"
  port        = 3000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id
  health_check {
    path    = "/"
    matcher = "200-399"
  }
  deregistration_delay = 30
  tags                 = var.tags
}

resource "aws_lb_target_group" "api" {
  name        = "${var.name}-api"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id
  health_check {
    path    = "/readyz"
    matcher = "200"
  }
  deregistration_delay = 30
  tags                 = var.tags
}

resource "aws_lb_target_group" "api_internal" {
  name        = "${var.name}-api-int"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id
  health_check {
    path    = "/readyz"
    matcher = "200"
  }
  deregistration_delay = 30
  tags                 = var.tags
}

resource "aws_lb_target_group" "otlp" {
  name        = "${var.name}-otlp"
  port        = 4319
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id
  health_check {
    path    = "/healthz"
    matcher = "200"
  }
  tags = var.tags
}

# A target group belongs to one load balancer: the gateway registers in both.
resource "aws_lb_target_group" "otlp_internal" {
  name        = "${var.name}-otlp-int"
  port        = 4319
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id
  health_check {
    path    = "/healthz"
    matcher = "200"
  }
  tags = var.tags
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.public.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.public.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn
  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "not found"
      status_code  = "404"
    }
  }
}

resource "aws_lb_listener_rule" "deny_internal_paths" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 1
  action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "not found"
      status_code  = "404"
    }
  }
  condition {
    path_pattern {
      values = ["/metrics", "/metrics/*", "/internal/*"]
    }
  }
}

resource "aws_lb_listener_rule" "hosts" {
  for_each = {
    app  = { priority = 10, tg = aws_lb_target_group.web.arn }
    api  = { priority = 20, tg = aws_lb_target_group.api.arn }
    otlp = { priority = 30, tg = aws_lb_target_group.otlp.arn }
  }
  listener_arn = aws_lb_listener.https.arn
  priority     = each.value.priority
  action {
    type             = "forward"
    target_group_arn = each.value.tg
  }
  condition {
    host_header {
      values = [local.hosts[each.key]]
    }
  }
}

# --- WAF on the public ALB ------------------------------------------------------------------
resource "aws_wafv2_web_acl" "public" {
  name  = "${var.name}-public"
  scope = "REGIONAL"
  default_action {
    allow {}
  }
  rule {
    name     = "rate-limit-per-ip"
    priority = 1
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit              = var.waf_rate_limit_per_5m
        aggregate_key_type = "IP"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-rate-limit"
      sampled_requests_enabled   = true
    }
  }
  rule {
    name     = "aws-ip-reputation"
    priority = 2
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesAmazonIpReputationList"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-ip-reputation"
      sampled_requests_enabled   = true
    }
  }
  rule {
    name     = "aws-common"
    priority = 3
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesCommonRuleSet"
        # OTLP batches, source archives and OpenAPI documents are legitimately large; the
        # API enforces its own body limits.
        rule_action_override {
          name = "SizeRestrictions_BODY"
          action_to_use {
            count {}
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-common"
      sampled_requests_enabled   = true
    }
  }
  rule {
    name     = "aws-known-bad-inputs"
    priority = 4
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-bad-inputs"
      sampled_requests_enabled   = true
    }
  }
  rule {
    name     = "aws-sqli"
    priority = 5
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesSQLiRuleSet"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-sqli"
      sampled_requests_enabled   = true
    }
  }
  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.name}-public"
    sampled_requests_enabled   = true
  }
  tags = var.tags
}

resource "aws_wafv2_web_acl_association" "public" {
  resource_arn = aws_lb.public.arn
  web_acl_arn  = aws_wafv2_web_acl.public.arn
}

# --- Runner path: PrivateLink → NLB → internal ALB ----------------------------------------
resource "aws_lb" "internal" {
  name                       = "${var.name}-runners"
  load_balancer_type         = "application"
  internal                   = true
  subnets                    = var.private_subnet_ids
  security_groups            = [aws_security_group.alb_internal.id]
  drop_invalid_header_fields = true
  idle_timeout               = 60 # lease long-polls are ≤ 20 s
  enable_deletion_protection = var.deletion_protection
  tags                       = var.tags
}

resource "aws_lb_listener" "internal" {
  load_balancer_arn = aws_lb.internal.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn
  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "not found"
      status_code  = "404"
    }
  }
}

resource "aws_lb_listener_rule" "runner_api" {
  listener_arn = aws_lb_listener.internal.arn
  priority     = 10
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api_internal.arn
  }
  condition {
    # Lease API, presigned artifact up/downloads proxied by the API, and nothing else.
    path_pattern {
      values = ["/internal/runner/*"]
    }
  }
}

resource "aws_lb_listener_rule" "runner_otlp" {
  listener_arn = aws_lb_listener.internal.arn
  priority     = 20
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.otlp_internal.arn
  }
  condition {
    path_pattern {
      values = ["/v1/traces", "/v1/metrics", "/v1/logs"]
    }
  }
}

resource "aws_lb" "privatelink" {
  name                             = "${var.name}-pl"
  load_balancer_type               = "network"
  internal                         = true
  subnets                          = var.private_subnet_ids
  enable_cross_zone_load_balancing = true
  enable_deletion_protection       = var.deletion_protection
  tags                             = var.tags
}

resource "aws_lb_target_group" "privatelink" {
  name        = "${var.name}-pl"
  port        = 443
  protocol    = "TCP"
  target_type = "alb"
  vpc_id      = var.vpc_id
  health_check {
    protocol = "HTTPS"
    path     = "/"
    matcher  = "200-499"
  }
  tags = var.tags
}

resource "aws_lb_target_group_attachment" "privatelink" {
  target_group_arn = aws_lb_target_group.privatelink.arn
  target_id        = aws_lb.internal.arn
  port             = 443
  depends_on       = [aws_lb_listener.internal]
}

resource "aws_lb_listener" "privatelink" {
  load_balancer_arn = aws_lb.privatelink.arn
  port              = 443
  protocol          = "TCP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.privatelink.arn
  }
}

resource "aws_vpc_endpoint_service" "runners" {
  acceptance_required        = false
  network_load_balancer_arns = [aws_lb.privatelink.arn]
  allowed_principals         = [for id in var.execution_account_ids : "arn:aws:iam::${id}:root"]
  private_dns_name           = local.hosts.runners
  tags                       = merge(var.tags, { Name = "${var.name}-runners" })
}

# --- DNS -------------------------------------------------------------------------------------
resource "aws_route53_record" "public" {
  for_each = var.route53_zone_id == null ? {} : { app = local.hosts.app, api = local.hosts.api, otlp = local.hosts.otlp }
  zone_id  = var.route53_zone_id
  name     = each.value
  type     = "A"
  alias {
    name                   = aws_lb.public.dns_name
    zone_id                = aws_lb.public.zone_id
    evaluate_target_health = true
  }
}

# PrivateLink private-DNS ownership proof for runners.<domain>.
resource "aws_route53_record" "runners_verification" {
  count   = var.route53_zone_id == null ? 0 : 1
  zone_id = var.route53_zone_id
  name    = aws_vpc_endpoint_service.runners.private_dns_name_configuration[0].name
  type    = aws_vpc_endpoint_service.runners.private_dns_name_configuration[0].type
  ttl     = 1800
  records = [aws_vpc_endpoint_service.runners.private_dns_name_configuration[0].value]
}
