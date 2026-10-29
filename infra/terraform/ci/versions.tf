# =====================================================================================================
# ci/versions.tf: Terraform and provider versions for the optional CI/CD stack
#
# What this file does
#   Pins the Terraform version and the AWS provider for infra/terraform/ci, and configures the
#   provider (region, the project tag on everything).
#
# Where it fits
#   infra/terraform/ci is a SEPARATE root, with its own state, applied once by a human with admin
#   credentials (docs/10-ci-cd.md, "One-time setup"). It creates what GitHub Actions needs to push
#   images and roll them out: ECR repositories, the GitHub OIDC provider and the CI role. The main
#   stack in infra/terraform does not depend on it, and the pipeline never runs Terraform.
#
# Step by step
#   1. Require Terraform >= 1.9 and hashicorp/aws 6.x, the same range as the main stack.
#   2. Configure the AWS provider for var.region and tag every resource project = <prefix>.
#
# Key terms
#   Root module: a folder you run `terraform init/plan/apply` in; it has its own state file.
#   Provider: the plugin that turns Terraform resources into AWS API calls.
#   default_tags: tags the provider adds to every taggable resource, used for cost and cleanup checks.
# =====================================================================================================

terraform {
  required_version = ">= 1.9"

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
      project   = var.name_prefix
      component = "ci"
    }
  }
}
