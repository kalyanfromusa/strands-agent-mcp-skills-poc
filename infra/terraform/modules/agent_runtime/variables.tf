# =====================================================================================================
# modules/agent_runtime/variables.tf: inputs of the agent runtime module
#
# What this file does
#   Declares what the agent runtime needs: naming, its code zip, the skills location, the model, the
#   Gateway URL, and who may call the agent.
#
# Where it fits
#   Filled in by the module "agent_runtime" block in infra/terraform/main.tf, from modules "data",
#   "identity" and "gateway" and the root variable model_id.
#
# Step by step
#   1. Receive the zip location and skills location from module "data".
#   2. Receive the model id and the Gateway URL.
#   3. Receive the login settings: which Cognito client and which groups may call the agent.
#
# Key terms
#   OIDC (OpenID Connect) discovery URL: a web address that describes how to check a login server's
#     tokens.
#   Cognito group: a named set of users; group names are written into each member's token.
# =====================================================================================================

# Naming and location, and where the code zip is stored in S3 (bucket, key and exact version).
variable "name_prefix" { type = string }
variable "region" { type = string }
variable "account_id" { type = string }
variable "bucket" { type = string }
variable "zip_key" { type = string }
variable "zip_version_id" { type = string }

variable "skills_prefix" {
  description = "Key prefix of the skills folder in the bucket (e.g. skills/)."
  type        = string
}

# skills_s3_uri: full skills address, e.g. s3://<bucket>/skills/. model_id: the Bedrock model.
# gateway_url: the Gateway's MCP address, where the agent finds its tools.
variable "skills_s3_uri" { type = string }
variable "model_id" { type = string }
variable "gateway_url" { type = string }

variable "discovery_url" {
  description = "Cognito OIDC discovery URL for the inbound JWT authorizer."
  type        = string
}

variable "allowed_client_ids" {
  description = "Cognito app clients whose user tokens may invoke the agent (the public users client)."
  type        = list(string)
}

variable "allowed_groups" {
  description = "A caller must be in at least one of these Cognito groups (cognito:groups claim)."
  type        = list(string)
}

# Copied into the agent's settings (not needed in passthrough mode, see main.tf), and log retention days.
variable "oauth_provider_name" { type = string }
variable "oauth_scopes" { type = list(string) }
variable "log_retention_days" { type = number }

# Folder of the repository on the computer running Terraform; scripts/log_group.py runs from here.
variable "repo_root" { type = string }
