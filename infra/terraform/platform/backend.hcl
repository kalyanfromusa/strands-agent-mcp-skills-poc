# Remote state for the platform stack: `terraform init -backend-config=backend.hcl`.
# The bucket is created by ../bootstrap (shopassist-tfstate-<account>-<region>). No secrets in here.
bucket       = "shopassist-tfstate-438750401181-us-east-1"
key          = "platform.tfstate"
region       = "us-east-1"
encrypt      = true
use_lockfile = true
