# Execution plane (ADR-0008, ADR-0009): an autoscaled pool of Docker + gVisor hosts in a
# separate AWS account. Hosts have no inbound access, reach the control plane only through a
# PrivateLink interface endpoint (runner lease API + OTLP), and egress to targets through NAT
# with fixed Elastic IPs. Instance metadata is IMDSv2 with hop limit 1 (containers cannot reach
# it); the instance role can only pull images and write its own logs.

variable "name" { type = string }
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "ami_id" {
  type        = string
  description = "Runner host AMI built by infra/packer/runner-host.pkr.hcl"
}
variable "instance_type" {
  type    = string
  default = "c7i.4xlarge"
}
variable "min_size" {
  type    = number
  default = 2
}
variable "max_size" {
  type    = number
  default = 24
}
variable "warm_pool_min" {
  type    = number
  default = 2
}
variable "control_plane_endpoint_service" {
  type        = string
  description = "PrivateLink endpoint service name exported by the control-plane account"
}
variable "runner_token_secret_arn" {
  type        = string
  description = "Secrets Manager ARN holding the runner signing bootstrap token"
}
variable "tags" {
  type    = map(string)
  default = {}
}

data "aws_region" "current" {}

resource "aws_security_group" "runner" {
  name        = "${var.name}-runner"
  description = "Runner hosts: no inbound; outbound HTTPS/HTTP for tests via NAT and PrivateLink"
  vpc_id      = var.vpc_id
  egress {
    description = "test traffic (allowlisted per run by the egress proxy) and control plane"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = var.tags
}

resource "aws_security_group" "endpoint" {
  name        = "${var.name}-cp-endpoint"
  description = "PrivateLink endpoint to the control plane"
  vpc_id      = var.vpc_id
  ingress {
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_security_group.runner.id]
  }
  tags = var.tags
}

resource "aws_vpc_endpoint" "control_plane" {
  vpc_id              = var.vpc_id
  service_name        = var.control_plane_endpoint_service
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.private_subnet_ids
  security_group_ids  = [aws_security_group.endpoint.id]
  private_dns_enabled = true
  tags                = var.tags
}

resource "aws_iam_role" "runner" {
  name = "${var.name}-runner"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "runner" {
  role = aws_iam_role.runner.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], Resource = "*" },
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "*" },
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = var.runner_token_secret_arn },
    ]
  })
}

resource "aws_iam_instance_profile" "runner" {
  name = "${var.name}-runner"
  role = aws_iam_role.runner.name
}

resource "aws_launch_template" "runner" {
  name_prefix   = "${var.name}-runner-"
  image_id      = var.ami_id
  instance_type = var.instance_type
  iam_instance_profile { arn = aws_iam_instance_profile.runner.arn }
  vpc_security_group_ids = [aws_security_group.runner.id]
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size = 200
      volume_type = "gp3"
      encrypted   = true
    }
  }
  user_data = base64encode(templatefile("${path.module}/user-data.sh.tftpl", {
    api_url                 = "https://${aws_vpc_endpoint.control_plane.dns_entry[0].dns_name}"
    runner_token_secret_arn = var.runner_token_secret_arn
    region                  = data.aws_region.current.name
  }))
  tag_specifications {
    resource_type = "instance"
    tags          = merge(var.tags, { Name = "${var.name}-runner", Role = "runner" })
  }
}

resource "aws_autoscaling_group" "runner" {
  name                  = "${var.name}-runner"
  vpc_zone_identifier   = var.private_subnet_ids
  min_size              = var.min_size
  max_size              = var.max_size
  max_instance_lifetime = 86400 # hosts are recycled at least daily (threat model A4)
  launch_template {
    id      = aws_launch_template.runner.id
    version = "$Latest"
  }
  warm_pool {
    pool_state                  = "Stopped"
    min_size                    = var.warm_pool_min
    max_group_prepared_capacity = var.warm_pool_min + 4
  }
  instance_refresh {
    strategy = "Rolling"
    preferences { min_healthy_percentage = 90 }
  }
  tag {
    key                 = "Role"
    value               = "runner"
    propagate_at_launch = true
  }
}

# Scale on the control plane's queue-depth metric (queued runs + jobs per free slot).
resource "aws_autoscaling_policy" "queue_depth" {
  name                   = "${var.name}-queue-depth"
  autoscaling_group_name = aws_autoscaling_group.runner.name
  policy_type            = "TargetTrackingScaling"
  target_tracking_configuration {
    target_value = 0.8
    customized_metric_specification {
      metric_name = "SlotUtilization"
      namespace   = "ReliabilityEngineer/Execution"
      statistic   = "Average"
    }
  }
}

output "runner_security_group_id" { value = aws_security_group.runner.id }
output "asg_name" { value = aws_autoscaling_group.runner.name }
output "runner_role_name" { value = aws_iam_role.runner.name }
