# =====================================================================================================
# modules/mcp_runtime/variables.tf: inputs of the support MCP server module
#
# What this file does
#   Declares the values the support server needs: naming, its container image, the tickets table and the
#   login (token-checking) settings.
#
# Where it fits
#   Filled in by the module "mcp_runtime" block in infra/terraform/main.tf, from modules "data" and
#   "identity".
#
# Step by step
#   1. Receive the image URI (root main.tf) and the tickets table (module "data").
#   2. Receive the Cognito discovery URL and the allowed client id (the M2M client) from "identity".
#
# Key terms
#   OIDC (OpenID Connect) discovery URL: a web address that describes how to check a login server's
#     tokens.
#   App client id: the id of a Cognito application; each token records which client it came from.
# =====================================================================================================

# Naming and location. name_prefix includes the environment, e.g. shopassist-prod.
variable "name_prefix" { type = string }
variable "region" { type = string }
variable "account_id" { type = string }

# The container image to run (<repo>:<git sha>), its ECR repository ARN (pull permission), and the
# permissions boundary every role must carry.
variable "image_uri" { type = string }
variable "ecr_repository_arn" { type = string }
variable "permissions_boundary_arn" { type = string }
# The tickets table: the name goes into the server's settings, the ARN into its permissions.
variable "tickets_table_name" { type = string }
variable "tickets_table_arn" { type = string }

variable "discovery_url" {
  description = "Cognito OIDC discovery URL for the inbound JWT authorizer."
  type        = string
}

variable "allowed_client_ids" {
  description = "Cognito app clients whose tokens may call this server (the M2M client used by the gateway)."
  type        = list(string)
}

# How many days CloudWatch keeps the server's logs.
variable "log_retention_days" { type = number }

# Folder of the repository on the computer running Terraform; scripts/log_group.py runs from here.
variable "repo_root" { type = string }
