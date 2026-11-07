# =====================================================================================================
# bootstrap/outputs.tf: what the other stacks need to know about the state bucket
#
# What this file does
#   Prints the bucket name and ARN after `terraform apply`.
#
# Where it fits
#   The bucket name is predictable (shopassist-tfstate-<account>-<region>), so it is written into the
#   committed backend files infra/terraform/platform/backend.hcl and infra/terraform/envs/<env>.backend.hcl.
#   The platform stack derives the same name to scope the CI roles' state access.
#
# Step by step
#   1. Terraform reads the values from the resources in main.tf.
#
# Key terms
#   Output: a value Terraform shows after apply, and that other tools can read with `terraform output`.
# =====================================================================================================

output "state_bucket_name" {
  description = "S3 bucket that holds the platform and main-stack state files."
  value       = aws_s3_bucket.state.bucket
}

output "state_bucket_arn" {
  description = "ARN of the state bucket."
  value       = aws_s3_bucket.state.arn
}
