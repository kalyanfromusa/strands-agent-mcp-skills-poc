# =====================================================================================================
# ci/outputs.tf: what to copy into the GitHub repository variables
#
# What this file does
#   Prints the CI role ARN, the ECR repository URLs and a ready-made list of `gh variable set`
#   commands, so the one-time GitHub setup is copy and paste.
#
# Where it fits
#   After `terraform apply` here: `terraform output -raw github_variables_commands` (docs/10-ci-cd.md).
#   The runtime and gateway ids come from the MAIN stack (.deploy/outputs.json), not from here.
#
# Step by step
#   1. ci_role_arn        -> repository variable AWS_ROLE_ARN.
#   2. ecr_repository_urls -> where the images go (informational; the workflow derives them).
#   3. github_variables_commands -> the commands for this stack's part of the variables.
#
# Key terms
#   Repository variable: a non-secret setting of a GitHub repository, read as ${{ vars.NAME }}.
# =====================================================================================================

output "ci_role_arn" {
  description = "Set as the GitHub repository variable AWS_ROLE_ARN."
  value       = aws_iam_role.ci.arn
}

output "ecr_repository_urls" {
  description = "ECR repository URLs (agent, support_mcp)."
  value       = { for k, r in aws_ecr_repository.this : k => r.repository_url }
}

output "oidc_provider_arn" {
  value = local.oidc_provider_arn
}

output "github_variables_commands" {
  description = "gh CLI commands for the variables this stack knows. Add the runtime/gateway ids from the main stack."
  value       = <<-EOT
    gh variable set AWS_ROLE_ARN --body "${aws_iam_role.ci.arn}"
    gh variable set AWS_REGION --body "${var.region}"
    gh variable set ECR_AGENT_REPOSITORY --body "${aws_ecr_repository.this["agent"].name}"
    gh variable set ECR_SUPPORT_MCP_REPOSITORY --body "${aws_ecr_repository.this["support_mcp"].name}"
    gh variable set ARTIFACTS_BUCKET --body "${local.bucket}"
  EOT
}
