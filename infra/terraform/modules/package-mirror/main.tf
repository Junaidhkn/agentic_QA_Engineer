# Package mirror for sandbox builds (threat model A6, ADR-0011): AWS CodeArtifact proxying
# public npm and PyPI. Sandbox builds may reach only this mirror (egress proxy allowlist), so
# installs are logged, cacheable and can be cut off centrally. Runner hosts get read-only
# access; tokens are fetched by the host agent and never enter sandbox containers.

variable "name" { type = string }
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "runner_role_name" {
  type        = string
  description = "IAM role of the runner hosts (execution-plane module output)"
}
variable "allowed_security_group_ids" {
  type        = list(string)
  description = "Security groups allowed to reach the CodeArtifact endpoints (runner hosts)"
}
variable "tags" {
  type    = map(string)
  default = {}
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

resource "aws_kms_key" "mirror" {
  description         = "${var.name} package mirror"
  enable_key_rotation = true
  tags                = var.tags
}

resource "aws_codeartifact_domain" "this" {
  domain         = var.name
  encryption_key = aws_kms_key.mirror.arn
  tags           = var.tags
}

resource "aws_codeartifact_repository" "npm_upstream" {
  repository = "npm-public"
  domain     = aws_codeartifact_domain.this.domain
  external_connections { external_connection_name = "public:npmjs" }
  tags = var.tags
}

resource "aws_codeartifact_repository" "pypi_upstream" {
  repository = "pypi-public"
  domain     = aws_codeartifact_domain.this.domain
  external_connections { external_connection_name = "public:pypi" }
  tags = var.tags
}

resource "aws_codeartifact_repository" "sandbox" {
  repository  = "sandbox"
  description = "What sandbox builds install from (npm + PyPI via upstreams)"
  domain      = aws_codeartifact_domain.this.domain
  upstream { repository_name = aws_codeartifact_repository.npm_upstream.repository }
  upstream { repository_name = aws_codeartifact_repository.pypi_upstream.repository }
  tags = var.tags
}

resource "aws_security_group" "endpoints" {
  name        = "${var.name}-mirror-endpoints"
  description = "CodeArtifact interface endpoints, reachable from runner hosts only"
  vpc_id      = var.vpc_id
  ingress {
    description     = "HTTPS from runner hosts"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = var.allowed_security_group_ids
  }
  tags = var.tags
}

resource "aws_vpc_endpoint" "codeartifact" {
  for_each            = toset(["codeartifact.api", "codeartifact.repositories"])
  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${data.aws_region.current.name}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.private_subnet_ids
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true
  tags                = var.tags
}

data "aws_iam_policy_document" "read" {
  statement {
    sid       = "Token"
    actions   = ["codeartifact:GetAuthorizationToken"]
    resources = [aws_codeartifact_domain.this.arn]
  }
  statement {
    sid       = "BearerToken"
    actions   = ["sts:GetServiceBearerToken"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "sts:AWSServiceName"
      values   = ["codeartifact.amazonaws.com"]
    }
  }
  statement {
    sid       = "Read"
    actions   = ["codeartifact:GetRepositoryEndpoint", "codeartifact:ReadFromRepository"]
    resources = [aws_codeartifact_repository.sandbox.arn]
  }
}

resource "aws_iam_role_policy" "runner_read" {
  name   = "${var.name}-mirror-read"
  role   = var.runner_role_name
  policy = data.aws_iam_policy_document.read.json
}

locals {
  base = "${aws_codeartifact_domain.this.domain}-${data.aws_caller_identity.current.account_id}.d.codeartifact.${data.aws_region.current.name}.amazonaws.com"
}

output "npm_registry" {
  description = "RA_NPM_REGISTRY"
  value       = "https://${local.base}/npm/${aws_codeartifact_repository.sandbox.repository}/"
}
output "pip_index_url" {
  description = "RA_PIP_INDEX_URL (the host agent adds the auth token)"
  value       = "https://${local.base}/pypi/${aws_codeartifact_repository.sandbox.repository}/simple/"
}
output "mirror_hosts" {
  description = "RA_MIRROR_HOSTS — the only egress allowed for sandbox builds"
  value       = [local.base]
}
