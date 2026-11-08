# =====================================================================================================
# envs/dev.backend.hcl: where Terraform keeps the state of the dev main stack
#
# What this file does
#   Names the S3 state bucket (created by ../bootstrap) and the state file main/dev.tfstate.
#
# Where it fits
#   Read by `terraform init -backend-config=envs/dev.backend.hcl` (the pipeline runs this in every
#   job; locally add -reconfigure to switch a folder between environments). dev is defined but not deployed
#   today, so this file has not been used for a real apply yet.
#   No secrets in here.
#
# Step by step
#   1. bucket/key/region: the state file s3://<bucket>/main/dev.tfstate in us-east-1.
#   2. encrypt: the state file is stored encrypted.
#   3. use_lockfile: Terraform writes main/dev.tfstate.tflock while it works, so two runs can't
#      change the dev stack at the same time.
#
# Key terms
#   State: Terraform's record of what it created (ids, ARNs, generated demo passwords). Sensitive.
#   Backend: where the state is stored.
# =====================================================================================================
bucket       = "shopassist-tfstate-438750401181-us-east-1"
key          = "main/dev.tfstate"
region       = "us-east-1"
encrypt      = true
use_lockfile = true
