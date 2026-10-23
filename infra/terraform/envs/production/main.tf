# Production execution plane (account B). Control-plane resources are added in Phase 10.
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
    tags = { Project = "reliability-engineer", Environment = "production", ManagedBy = "terraform" }
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
  name   = "re-production-exec"
  cidr   = "10.30.0.0/16"
  azs    = ["${var.region}a", "${var.region}b", "${var.region}c"]
}

module "execution_plane" {
  source                         = "../../modules/execution-plane"
  name                           = "re-production"
  vpc_id                         = module.exec_network.vpc_id
  private_subnet_ids             = module.exec_network.private_subnet_ids
  ami_id                         = var.runner_ami_id
  instance_type                  = "c7i.4xlarge"
  min_size                       = 2
  max_size                       = 24
  warm_pool_min                  = 2
  control_plane_endpoint_service = var.control_plane_endpoint_service
  runner_token_secret_arn        = var.runner_token_secret_arn
}

output "egress_ips" {
  description = "Publish these (RE_EGRESS_IPS) so customers can allowlist test traffic"
  value       = module.exec_network.egress_ips
}
