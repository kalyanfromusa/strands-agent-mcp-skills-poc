# =====================================================================================================
# modules/role_mapper/variables.tf: inputs of the role-mapper Lambda
#
# What this file does
#   Naming, the container image, the permissions boundary, and the mapping settings.
#
# Where it fits
#   Filled in by module "role_mapper" in infra/terraform/main.tf from the root variables
#   (entra_role_source, entra_role_map) and the platform outputs.
#
# Key terms
#   role_map: Entra value (app role value, or group object id in "groups" mode) => ShopAssist group.
# =====================================================================================================

variable "name_prefix" { type = string }
variable "image_uri" { type = string }
variable "permissions_boundary_arn" { type = string }
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
