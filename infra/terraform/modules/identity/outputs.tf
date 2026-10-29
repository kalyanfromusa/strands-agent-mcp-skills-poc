# =====================================================================================================
# modules/identity/outputs.tf: what the identity module hands to the other modules
#
# What this file does
#   Publishes the Cognito ids and addresses, the credential provider's name, ARN and secret location,
#   and the demo users' login details.
#
# Where it fits
#   The root main.tf passes these to mcp_runtime, gateway and agent_runtime (to check tokens and to
#   fetch the Gateway's machine token), and writes them to .deploy/outputs.json and .deploy/users.json.
#
# Step by step
#   1. Report the user pool id, discovery URL and token URL.
#   2. Report the two app client ids (M2M client and users client).
#   3. Report the credential provider's name, ARN and the ARN of the secret it stores.
#   4. Report the demo users' usernames, passwords and groups, once their groups are set up.
#
# Key terms
#   Client id: the public id of a Cognito app client; it is written into every token it issues.
#   Secrets Manager: the AWS service that stores secrets such as the M2M client secret, encrypted.
#   Sensitive output: a value Terraform hides on screen (it still sits in the state file).
# =====================================================================================================

output "user_pool_id" { value = aws_cognito_user_pool.this.id }

# The address the authorizers use to learn how to verify tokens. Published only after the domain
# exists, because before that the address does not yet list the token endpoint.
output "discovery_url" {
  value      = local.discovery_url
  depends_on = [aws_cognito_user_pool_domain.this]
}

# token_url: where a client trades its credentials for a token.
# client_id: the M2M client. users_client_id: the client people log in through.
output "token_url" { value = local.token_url }
output "client_id" { value = aws_cognito_user_pool_client.m2m.id }
output "users_client_id" { value = aws_cognito_user_pool_client.users.id }
output "oauth_provider_name" { value = aws_bedrockagentcore_oauth2_credential_provider.cognito.name }
output "oauth_provider_arn" { value = aws_bedrockagentcore_oauth2_credential_provider.cognito.credential_provider_arn }

# Where AgentCore stored the M2M client secret. The Gateway's role needs permission to read it.
# If the provider does not report it, fall back to a pattern that matches AgentCore's secrets in
# this account and region.
output "oauth_provider_secret_arn" {
  # Fallback: AgentCore names its managed secrets bedrock-agentcore-identity!<...>.
  value = try(
    aws_bedrockagentcore_oauth2_credential_provider.cognito.client_secret_arn[0].secret_arn,
    "arn:aws:secretsmanager:${var.region}:${var.account_id}:secret:bedrock-agentcore-identity!*"
  )
}

# The demo logins, e.g. { alice = { username, password, group = "shop-admin" }, ... }.
# The root main.tf writes this to .deploy/users.json.
output "demo_users" {
  description = "design.md section 8 format for .deploy/users.json."
  sensitive   = true
  value = {
    for u, g in var.demo_users : u => {
      username = aws_cognito_user.this[u].username
      password = random_password.user[u].result
      group    = g
    }
  }
  # Only hand out credentials once the group memberships exist.
  depends_on = [aws_cognito_user_in_group.this]
}
