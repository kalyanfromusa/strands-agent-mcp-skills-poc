# Remote state of the dev main stack: `terraform init -reconfigure -backend-config=envs/dev.backend.hcl`.
# The bucket is created by ../bootstrap. No secrets in here.
bucket       = "shopassist-tfstate-438750401181-us-east-1"
key          = "main/dev.tfstate"
region       = "us-east-1"
encrypt      = true
use_lockfile = true
