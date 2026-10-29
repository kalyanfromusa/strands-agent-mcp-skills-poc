# =====================================================================================================
# outputs.tf: the values Terraform prints at the end of `terraform apply`
#
# What this file does
#   Lists the ids and addresses of what was created, plus where the two .deploy/ files were written
#   and the commands to try the deployment.
#
# Where it fits
#   Shown on screen after `apply`, and available later with `terraform output`. The same values are
#   also written to .deploy/outputs.json (see main.tf), which is what the scripts actually read.
#
# Step by step
#   1. Repeat each key of .deploy/outputs.json as a Terraform output.
#   2. Print the full paths of .deploy/outputs.json and .deploy/users.json.
#   3. Print the two smoke-test commands to run from the repository root.
#
# Key terms
#   Output: a value Terraform reports after it runs, like the "result" cells of a spreadsheet.
#   Smoke test: a quick end-to-end check that the deployment works at all.
# =====================================================================================================

# Same keys as .deploy/outputs.json (design.md section 6).
output "region" { value = var.region }
output "account_id" { value = local.account_id }
output "artifacts_bucket" { value = module.data.artifacts_bucket }
output "skills_s3_uri" { value = module.data.skills_s3_uri }
output "agent_runtime_arn" { value = module.agent_runtime.runtime_arn }
output "support_mcp_runtime_arn" { value = module.mcp_runtime.runtime_arn }
output "gateway_id" { value = module.gateway.gateway_id }
output "gateway_url" { value = module.gateway.gateway_url }
output "cognito_user_pool_id" { value = module.identity.user_pool_id }
output "cognito_client_id" { value = module.identity.client_id }
output "cognito_token_url" { value = module.identity.token_url }
output "oauth_provider_name" { value = module.identity.oauth_provider_name }
output "cognito_users_client_id" { value = module.identity.users_client_id }
output "policy_engine_id" { value = module.gateway.policy_engine_id }
output "deployed_by" { value = "terraform" }

# Full path of .deploy/outputs.json on your computer.
output "deploy_outputs_file" {
  description = "Where the contract file was written."
  value       = abspath(local_file.deploy_outputs.filename)
}

# Full path of .deploy/users.json. Only the path is printed, never the passwords inside it.
output "users_file" {
  description = "Demo user credentials (alice/bob/carol)."
  value       = abspath(local_sensitive_file.demo_users.filename)
}

# Copy-paste commands: ask the agent a question as alice, then compare what alice, bob and carol may do.
output "invoke_command" {
  description = "Smoke test from the repo root. The agent runtime only accepts Cognito user tokens (no SigV4), so use the script, which reads .deploy/outputs.json and .deploy/users.json."
  value       = <<-EOT
    uv run python scripts/invoke_agent.py --user alice --prompt "Where is my order 1042?"
    uv run python scripts/demo_access.py
  EOT
}
