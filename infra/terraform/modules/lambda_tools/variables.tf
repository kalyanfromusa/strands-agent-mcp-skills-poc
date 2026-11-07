# =====================================================================================================
# modules/lambda_tools/variables.tf: inputs of the orders Lambda module
#
# What this file does
#   Declares the values the Lambda module needs: naming, its container image, and its two tables.
#
# Where it fits
#   Filled in by the module "lambda_tools" block in infra/terraform/main.tf, mostly from module "data".
#
# Step by step
#   1. Receive the image URI and the permissions boundary from the root main.tf.
#   2. Receive the orders and returns table names (for the code) and ARNs (for the permissions).
#
# Key terms
#   ARN (Amazon Resource Name): the unique id of an AWS resource, used in permission rules.
# =====================================================================================================

# Naming (name_prefix includes the environment, e.g. shopassist-prod), the container image
# (<repo>:<git sha>), and the permissions boundary every role must carry.
variable "name_prefix" { type = string }
variable "image_uri" { type = string }
variable "permissions_boundary_arn" { type = string }
# The orders and returns tables: names go into the function's settings, ARNs into its permissions.
variable "orders_table_name" { type = string }
variable "orders_table_arn" { type = string }
variable "returns_table_name" { type = string }
variable "returns_table_arn" { type = string }
# How many days CloudWatch keeps the function's logs.
variable "log_retention_days" { type = number }
