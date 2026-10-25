# Staging control plane (account A, operations.md §1). The execution plane is in
# envs/staging (account B) and connects through the PrivateLink endpoint service exported here.
terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }
  backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "reliability-engineer", Environment = "staging", ManagedBy = "terraform" }
  }
}

provider "aws" {
  alias  = "dr"
  region = var.dr_region
  default_tags {
    tags = { Project = "reliability-engineer", Environment = "staging", ManagedBy = "terraform" }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}
variable "dr_region" {
  type    = string
  default = "us-west-2"
}
variable "domain" { type = string }
variable "certificate_arn" { type = string }
variable "route53_zone_id" {
  type    = string
  default = null
}
variable "image_repository" { type = string }
variable "image_tag" { type = string }
variable "execution_account_id" { type = string }
variable "redis_auth_token" {
  type      = string
  sensitive = true
}
variable "telemetry" {
  type = object({
    mimir_url          = string
    tempo_url          = string
    loki_url           = string
    otel_collector_url = string
  })
}
variable "canary" {
  type = object({
    project_id = string
    target_id  = string
  })
  default = null
}
variable "alarm_email" {
  type    = string
  default = null
}
variable "pager_https_endpoint" {
  type    = string
  default = null
}

module "network" {
  source              = "../../modules/network"
  name                = "re-staging-cp"
  cidr                = "10.21.0.0/16"
  azs                 = ["${var.region}a", "${var.region}b"]
  create_data_subnets = true
}

module "control_plane" {
  source = "../../modules/control-plane"
  providers = {
    aws    = aws
    aws.dr = aws.dr
  }
  name                  = "re-staging"
  environment           = "staging"
  vpc_id                = module.network.vpc_id
  vpc_cidr              = module.network.cidr
  public_subnet_ids     = module.network.public_subnet_ids
  private_subnet_ids    = module.network.private_subnet_ids
  data_subnet_ids       = module.network.data_subnet_ids
  domain                = var.domain
  certificate_arn       = var.certificate_arn
  route53_zone_id       = var.route53_zone_id
  image_repository      = var.image_repository
  image_tag             = var.image_tag
  execution_account_ids = [var.execution_account_id]
  redis_auth_token      = var.redis_auth_token
  telemetry             = var.telemetry
  canary                = var.canary
  alarm_email           = var.alarm_email
  pager_https_endpoint  = var.pager_https_endpoint
  # "Full topology, smaller sizes" (operations §2).
  db_instance_class   = "db.t4g.large"
  db_multi_az         = false
  redis_node_type     = "cache.t4g.medium"
  deletion_protection = false
  services = {
    web            = { min = 1, max = 2 }
    api            = { min = 2, max = 3 }
    worker-default = { min = 1, max = 2 }
    worker-agents  = { min = 1, max = 2, cpu = 1024, memory = 2048 }
    otlp-gateway   = { min = 1, max = 2 }
  }
}

output "runner_endpoint_service_name" {
  description = "control_plane_endpoint_service for envs/staging"
  value       = module.control_plane.runner_endpoint_service_name
}
output "hosts" { value = module.control_plane.hosts }
output "migrate" {
  description = "aws ecs run-task inputs for the pre-deploy migration"
  value = {
    cluster         = module.control_plane.cluster_name
    task_definition = module.control_plane.migrate_task_definition
    subnets         = module.control_plane.task_subnets
    security_group  = module.control_plane.task_security_group_id
  }
}
