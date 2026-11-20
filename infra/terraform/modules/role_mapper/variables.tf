# =====================================================================================================
# modules/role_mapper/variables.tf: inputs of the role-mapper Lambda
#
# What this file does
#   Declares what the role-mapper Lambda needs: naming, the container image, the permissions boundary,
#   log retention, and the mapping settings that become the function's environment variables.
#
# Where it fits
#   Filled in by module "role_mapper" in infra/terraform/main.tf from the root variables
#   (entra_role_source, entra_role_map, log_retention_days) and the platform outputs (image,
#   boundary). provider_name is fixed to "EntraID" there, the same name modules/identity gives the
#   Entra identity provider.
#
# Step by step
#   1. Receive the naming, image and boundary from the root main.tf.
#   2. Receive the mapping settings, which main.tf in this folder passes to the code unchanged.
#
# Key terms
#   role_map: Entra value (app role value, or group object id in "groups" mode) => ShopAssist group.
#   Permissions boundary: the ceiling on what a role may do, set by the platform stack.
# =====================================================================================================

# Naming (name_prefix includes the environment, e.g. shopassist-prod), the container image
# (<repo>:<git sha>), and the permissions boundary the function's role must carry.
variable "name_prefix" { type = string }
variable "image_uri" { type = string }
variable "permissions_boundary_arn" { type = string }
# How many days CloudWatch keeps the function's logs.
variable "log_retention_days" { type = number }

variable "provider_name" {
  description = "Name of the Cognito identity provider whose users are mapped (others pass through)."
  type        = string
}

variable "role_source" {
  description = "roles = Entra app roles (custom:entra_roles); groups = group object ids (custom:entra_groups)."
  type        = string
}

variable "role_map" {
  description = "Entra value => Cognito group (Group-ShopAssist-Admin / Group-ShopAssist-Viewer)."
  type        = map(string)
}
