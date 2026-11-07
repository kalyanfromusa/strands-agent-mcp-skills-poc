# Remote state of the prod main stack: `terraform init -reconfigure -backend-config=envs/prod.backend.hcl`.
# The bucket is created by ../bootstrap. No secrets in here.
bucket       = "shopassist-tfstate-438750401181-us-east-1"
key          = "main/prod.tfstate"
region       = "us-east-1"
encrypt      = true
use_lockfile = true
