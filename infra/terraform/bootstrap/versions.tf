# =====================================================================================================
# bootstrap/versions.tf: Terraform and provider versions, plus the AWS connection for this stack
#
# What this file does
#   Pins Terraform >= 1.10 (the S3 backend's native lock file, used by the other stacks, needs it) and
#   the AWS provider, and tags every resource with project, component and managed-by.
#
# Where it fits
#   There's no `backend` block: this stack's own state stays in a local file, because the remote bucket
#   is what this stack creates. Credentials come from the environment, e.g. AWS_PROFILE=personal.
#
# Step by step
#   1. `terraform init` downloads the AWS provider within the allowed version range.
#   2. The provider block sets the region and the default tags.
#
# Key terms
#   Provider: the Terraform plugin that talks to AWS.
#   Default tags: tags added automatically to every resource that supports tags.
# =====================================================================================================

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.71, < 7.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project    = var.name_prefix
      component  = "bootstrap"
      managed-by = "terraform"
    }
  }
}
