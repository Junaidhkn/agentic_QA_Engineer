terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source = "hashicorp/aws"
      # aws.dr: second region for backup replication and audit-anchor copies (operations §7).
      configuration_aliases = [aws.dr]
    }
  }
}
