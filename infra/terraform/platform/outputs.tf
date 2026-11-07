# =====================================================================================================
# platform/outputs.tf: values the main stack and GitHub need from the platform stack
#
# What this file does
#   Exposes the ECR repository URLs, the CI role ARNs, the permissions-boundary ARNs, and ready-made
#   `gh variable set` commands for the GitHub repository settings.
#
# Where it fits
#   The main stack reads ecr_repository_urls and boundary_arns from this stack's state
#   (terraform_remote_state, key platform.tfstate). You run the printed gh commands once (task 6.6).
#   None of these values is secret: ARNs and URLs identify resources but grant nothing on their own.
#
# Step by step
#   1. Terraform collects the values after apply; `terraform output` prints them again any time.
#
# Key terms
#   Repository variable: a non-secret setting stored in GitHub and readable by workflows as vars.NAME.
# =====================================================================================================

output "ecr_repository_urls" {
  description = "ECR repository URL per image (agent, support_mcp, orders_lambda, role_mapper)."
  value       = { for k, r in aws_ecr_repository.this : k => r.repository_url }
}

output "plan_role_arn" {
  description = "Role for terraform plan (PRs from this repo and main)."
  value       = aws_iam_role.plan.arn
}

output "apply_role_arns" {
  description = "Role for terraform apply, per environment."
  value       = { for e, r in aws_iam_role.apply : e => r.arn }
}

output "boundary_arns" {
  description = "Permissions boundary that every main-stack role of the environment must carry."
  value       = { for e, p in aws_iam_policy.boundary : e => p.arn }
}

output "oidc_provider_arn" {
  description = "GitHub OIDC provider in this account."
  value       = local.oidc_provider_arn
}

output "state_bucket" {
  description = "Terraform state bucket (created by ../bootstrap)."
  value       = local.state_bucket
}

output "github_variables_commands" {
  description = "Run once with the gh CLI (task 6.6). Repository variables are visible to workflows, not secret."
  value       = <<-EOT
    gh variable set AWS_REGION      --body "${var.region}"
    gh variable set AWS_PLAN_ROLE   --body "${aws_iam_role.plan.arn}"
    gh variable set TF_STATE_BUCKET --body "${local.state_bucket}"
    %{for e, r in aws_iam_role.apply~}
    gh variable set AWS_APPLY_ROLE --env ${e} --body "${r.arn}"
    %{endfor~}
    gh variable set DEPLOY_DEV      --body "false"
  EOT
}
