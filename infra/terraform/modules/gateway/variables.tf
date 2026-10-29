# =====================================================================================================
# modules/gateway/variables.tf: inputs of the Gateway module
#
# What this file does
#   Declares what the Gateway needs: naming, the token-checking settings, the two tool providers
#   (Lambda and MCP runtime), the credential provider for the outbound machine token, and the Cedar
#   policy files.
#
# Where it fits
#   Filled in by the module "gateway" block in infra/terraform/main.tf, from modules "identity",
#   "lambda_tools" and "mcp_runtime".
#
# Step by step
#   1. Receive the Cognito settings that decide who may call the Gateway at all.
#   2. Receive the orders Lambda ARN and its tool definitions file.
#   3. Receive the support runtime ARN and the credential provider used to call it.
#   4. Receive the policy folder and the M2M client id to fill into the policy templates.
#
# Key terms
#   OIDC (OpenID Connect) discovery URL: a web address that describes how to check a login server's
#     tokens.
#   Scope: a named permission requested in a machine token, here "<prefix>/invoke".
#   M2M (machine-to-machine) client: the Cognito client a service uses to log in as itself.
# =====================================================================================================

# Naming and location.
variable "name_prefix" { type = string }
variable "region" { type = string }
variable "account_id" { type = string }

variable "discovery_url" {
  description = "Cognito OIDC discovery URL for the inbound JWT authorizer."
  type        = string
}

variable "allowed_client_ids" {
  description = "Cognito app clients accepted inbound (users client for passthrough, M2M client for local dev)."
  type        = list(string)
}

# The orders Lambda that the `orders` target calls.
variable "lambda_arn" { type = string }

variable "tools_schema_path" {
  description = "JSON list of {name, description, inputSchema{type, properties, required}} for the orders Lambda target."
  type        = string
}

# The support MCP server on AgentCore Runtime that the `support` target calls.
variable "mcp_runtime_arn" { type = string }

variable "oauth_provider_arn" {
  description = "AgentCore Identity credential provider used for the outbound token to the support MCP runtime."
  type        = string
}

variable "oauth_provider_secret" {
  description = "Secrets Manager ARN holding the provider's client secret."
  type        = string
}

# The scopes the Gateway asks for in its machine token, e.g. ["shopassist/invoke"].
variable "oauth_scopes" { type = list(string) }

variable "policies_dir" {
  description = "Folder with *.cedar templates (placeholders {{GATEWAY_ARN}}, {{M2M_CLIENT_ID}})."
  type        = string
}

variable "m2m_client_id" {
  description = "Substituted for {{M2M_CLIENT_ID}} in the Cedar templates."
  type        = string
}
