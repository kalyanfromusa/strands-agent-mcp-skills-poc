# =====================================================================================================
# platform/versions.tf: Terraform and provider versions, the remote state location, the AWS connection
#
# What this file does
#   Pins Terraform >= 1.10 and the AWS provider, keeps this stack's state in the S3 state bucket
#   (created by ../bootstrap), and tags every resource.
#
# Where it fits
#   The `backend "s3"` block is deliberately partial: the bucket, key and region come from backend.hcl,
#   so you run `terraform init -backend-config=backend.hcl`. Credentials come from the environment
#   (AWS_PROFILE=personal on your machine; this stack is applied by hand, not by the pipeline).
#
# Step by step
#   1. `terraform init -backend-config=backend.hcl` connects to the state bucket and downloads the provider.
#   2. The provider block sets the region and the default tags.
#
# Key terms
#   Partial backend configuration: backend settings supplied at `init` time from a file, so the same
#     code can point at a different bucket without edits.
#   use_lockfile: Terraform writes <key>.tflock next to the state while it works, so two runs can't
#     change the same stack at the same time (no DynamoDB lock table needed).
# =====================================================================================================

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.71, < 7.0"
    }
  }

  # bucket, key, region and use_lockfile come from backend.hcl.
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project    = var.name_prefix
      component  = "platform"
      managed-by = "terraform"
    }
  }
}
