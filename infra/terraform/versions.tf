# =====================================================================================================
# versions.tf: which Terraform version and which plugins ("providers") this deployment needs
#
# What this file does
#   Pins the minimum Terraform version and the provider plugins Terraform downloads on `terraform init`.
#
# Where it fits
#   `terraform init` reads this first and downloads the providers into .terraform/ (and records the
#   exact versions in .terraform.lock.hcl). Every other file then uses these providers.
#
# Step by step
#   1. Refuse to run on Terraform older than 1.9.
#   2. Download the four providers listed below, within the allowed version ranges.
#   3. Keep the state (the record of what was created) in S3, one file per environment.
#
# Key terms
#   Provider: a plugin that knows how to talk to one system. "aws" creates AWS resources; "local"
#     writes files on your computer; "random" makes passwords; "time" adds deliberate pauses.
#   State: Terraform's record of what it created, kept in terraform.tfstate (git-ignored).
#   Backend: where the state is stored: the S3 state bucket, key main/<env>.tfstate, with a lock file.
# =====================================================================================================

terraform {
  required_version = ">= 1.10" # cross-variable validation (1.9) and the S3 backend lock file (1.10)

  required_providers {
    # AWS provider: creates every cloud resource (S3, DynamoDB, Lambda, Cognito, AgentCore, IAM, ...).
    aws = {
      source = "hashicorp/aws"
      # 6.71 is the first version whose bedrockagentcore_* schemas match .ref/tf_agentcore_schema.*
      version = ">= 6.71, < 7.0"
    }
    # Local provider: writes .deploy/outputs.json and .deploy/users.json on your computer.
    local = {
      source  = "hashicorp/local"
      version = ">= 2.5"
    }
    # Random provider: generates the demo users' passwords.
    random = {
      source  = "hashicorp/random"
      version = ">= 3.6"
    }
    # Time provider: the short time_sleep pauses that give new IAM roles time to become usable.
    time = {
      source  = "hashicorp/time"
      version = ">= 0.12"
    }
  }

  # Remote state in the S3 state bucket (created by ../bootstrap), one file per environment.
  # bucket, key, region and use_lockfile come from envs/<env>.backend.hcl:
  #   terraform init -backend-config=envs/prod.backend.hcl
  backend "s3" {}
}
