# =====================================================================================================
# envs/prod.backend.hcl: where Terraform keeps the state of the prod main stack
#
# What this file does
#   Names the S3 state bucket (created by ../bootstrap) and the state file main/prod.tfstate.
#
# Where it fits
#   Read by `terraform init -backend-config=envs/prod.backend.hcl` (the pipeline runs this in every
#   job; locally add -reconfigure to switch a folder between environments). prod is the deployed environment.
#   No secrets in here.
#
# Step by step
#   1. bucket/key/region: the state file s3://<bucket>/main/prod.tfstate in us-east-1.
#   2. encrypt: the state file is stored encrypted.
#   3. use_lockfile: Terraform writes main/prod.tfstate.tflock while it works, so two runs can't
#      change the prod stack at the same time.
#
# Key terms
#   State: Terraform's record of what it created (ids, ARNs, generated demo passwords). Sensitive.
#   Backend: where the state is stored.
# =====================================================================================================
bucket       = "shopassist-tfstate-438750401181-us-east-1"
key          = "main/prod.tfstate"
region       = "us-east-1"
encrypt      = true
use_lockfile = true
