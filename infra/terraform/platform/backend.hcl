# =====================================================================================================
# platform/backend.hcl: where Terraform keeps the state of the shared platform stack
#
# What this file does
#   Names the S3 state bucket (created by ../bootstrap, shopassist-tfstate-<account>-<region>) and the
#   state file platform.tfstate.
#
# Where it fits
#   Read by `terraform init -backend-config=backend.hcl` in this folder. The main stack reads the same
#   platform.tfstate (read-only) to find the ECR repositories and permissions boundaries. No secrets.
#
# Step by step
#   1. bucket/key/region: the state file s3://<bucket>/platform.tfstate in us-east-1.
#   2. encrypt: the state file is stored encrypted.
#   3. use_lockfile: Terraform writes platform.tfstate.tflock while it works, so two runs can't change
#      the platform at the same time.
#
# Key terms
#   State: Terraform's record of what it created. Backend: where that record is stored.
# =====================================================================================================
bucket       = "shopassist-tfstate-438750401181-us-east-1"
key          = "platform.tfstate"
region       = "us-east-1"
encrypt      = true
use_lockfile = true
