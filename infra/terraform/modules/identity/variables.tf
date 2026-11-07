# =====================================================================================================
# modules/identity/variables.tf: inputs of the identity (login) module
#
# What this file does
#   Declares the naming inputs and the list of demo users with the Cognito group each one joins.
#
# Where it fits
#   Filled in by the module "identity" block in infra/terraform/main.tf (which passes only the naming
#   values, so demo_users keeps its default). Read by modules/identity/main.tf and outputs.tf.
#
# Step by step
#   1. Receive the name prefix, region and account id from the root module.
#   2. Use the default demo_users list unless a caller overrides it.
#
# Key terms
#   Cognito group: a named set of users; the group name is written into each member's login token.
# =====================================================================================================

# Used to name the user pool, its domain and the clients.
# name_prefix includes the environment, e.g. shopassist-prod.
variable "name_prefix" { type = string }

# The OAuth resource-server id: scopes are "<api_identifier>/invoke". Same in every environment.
variable "api_identifier" {
  type    = string
  default = "shopassist"
}
variable "region" { type = string }
variable "account_id" { type = string }

# The three demo personas that show the access levels:
#   alice -> Group-ShopAssist-Admin  (may use every tool)
#   bob   -> Group-ShopAssist-Viewer (read-only tools only)
#   carol -> no group    (the agent refuses her before doing anything)
variable "demo_users" {
  description = "Demo users => Cognito group (\"\" = no group). design.md section 8."
  type        = map(string)
  default = {
    alice = "Group-ShopAssist-Admin"
    bob   = "Group-ShopAssist-Viewer"
    carol = ""
  }
}
