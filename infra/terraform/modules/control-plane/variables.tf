# Control plane (operations.md §1): ECS Fargate services behind a public ALB with WAF,
# RDS PostgreSQL Multi-AZ with PITR, ElastiCache Redis, S3 (artifacts, audit anchors with
# Object Lock, LGTM data), a PrivateLink endpoint service for the execution plane, CloudWatch
# alarms, and the weekly restore drill as a scheduled ECS task.

variable "name" {
  type        = string
  description = "Resource name prefix, e.g. re-production"
}
variable "environment" {
  type = string
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production"
  }
}
variable "vpc_id" { type = string }
variable "vpc_cidr" { type = string }
variable "public_subnet_ids" { type = list(string) }
variable "private_subnet_ids" { type = list(string) }
variable "data_subnet_ids" { type = list(string) }

variable "domain" {
  type        = string
  description = "Base domain: app.<domain>, api.<domain>, otlp.<domain>, runners.<domain>"
}
variable "certificate_arn" {
  type        = string
  description = "ACM certificate covering *.<domain>"
}
variable "image_repository" {
  type        = string
  description = "ECR registry prefix, e.g. 123456789012.dkr.ecr.us-east-1.amazonaws.com/reliability"
}
variable "image_tag" {
  type        = string
  description = "Immutable image tag (git sha) deployed to every service"
}
variable "execution_account_ids" {
  type        = list(string)
  description = "AWS accounts whose runner hosts may connect over PrivateLink"
}

# --- Sizing (operations §1 table; production defaults) ---------------------------------
variable "services" {
  description = "Per-service overrides of cpu, memory, min, max (see local.service_defaults)"
  type = map(object({
    cpu    = optional(number)
    memory = optional(number)
    min    = optional(number)
    max    = optional(number)
  }))
  default = {}
}
variable "db_instance_class" {
  type    = string
  default = "db.r7g.large"
}
variable "db_allocated_storage_gb" {
  type    = number
  default = 100
}
variable "db_multi_az" {
  type    = bool
  default = true
}
variable "db_backup_retention_days" {
  type    = number
  default = 14
}
variable "redis_node_type" {
  type    = string
  default = "cache.r7g.large"
}
variable "redis_auth_token" {
  type        = string
  sensitive   = true
  description = "ElastiCache AUTH token (16–128 chars), provided by CI from the secret store"
}

# --- Safety / operations -----------------------------------------------------------------
variable "deletion_protection" {
  type    = bool
  default = true
}
variable "log_retention_days" {
  type    = number
  default = 30
}
variable "waf_rate_limit_per_5m" {
  type        = number
  default     = 3000
  description = "Requests per 5 minutes per client IP before WAF blocks"
}
variable "alarm_email" {
  type    = string
  default = null
}
variable "pager_https_endpoint" {
  type        = string
  default     = null
  description = "PagerDuty (or similar) CloudWatch integration URL"
}
variable "restore_drill_schedule" {
  type    = string
  default = "cron(17 4 ? * SUN *)"
}
variable "tags" {
  type    = map(string)
  default = {}
}
