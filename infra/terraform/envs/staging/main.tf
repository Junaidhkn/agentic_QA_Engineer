# Staging execution plane (account B). The control plane (account A) is envs/staging-control-plane.
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

variable "region" {
  type    = string
  default = "us-east-1"
}
variable "runner_ami_id" { type = string }
variable "control_plane_endpoint_service" { type = string }
variable "runner_token_secret_arn" { type = string }

module "exec_network" {
  source = "../../modules/network"
  name   = "re-staging-exec"
  cidr   = "10.20.0.0/16"
  azs    = ["${var.region}a", "${var.region}b"]
}

module "execution_plane" {
  source                         = "../../modules/execution-plane"
  name                           = "re-staging"
  vpc_id                         = module.exec_network.vpc_id
  private_subnet_ids             = module.exec_network.private_subnet_ids
  ami_id                         = var.runner_ami_id
  instance_type                  = "c7i.2xlarge"
  min_size                       = 1
  max_size                       = 4
  warm_pool_min                  = 1
  control_plane_endpoint_service = var.control_plane_endpoint_service
  runner_token_secret_arn        = var.runner_token_secret_arn
}

module "package_mirror" {
  source                     = "../../modules/package-mirror"
  name                       = "re-staging"
  vpc_id                     = module.exec_network.vpc_id
  private_subnet_ids         = module.exec_network.private_subnet_ids
  runner_role_name           = module.execution_plane.runner_role_name
  allowed_security_group_ids = [module.execution_plane.runner_security_group_id]
}

output "package_mirror" {
  description = "RA_PIP_INDEX_URL / RA_NPM_REGISTRY / RA_MIRROR_HOSTS for runner hosts"
  value = {
    pip_index_url = module.package_mirror.pip_index_url
    npm_registry  = module.package_mirror.npm_registry
    mirror_hosts  = module.package_mirror.mirror_hosts
  }
}

output "egress_ips" {
  description = "Publish these (RE_EGRESS_IPS) so customers can allowlist test traffic"
  value       = module.exec_network.egress_ips
}
