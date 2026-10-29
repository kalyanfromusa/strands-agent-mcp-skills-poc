# =====================================================================================================
# modules/lambda_tools/variables.tf: inputs of the orders Lambda module
#
# What this file does
#   Declares the values the Lambda module needs: naming, where its code zip is, and its two tables.
#
# Where it fits
#   Filled in by the module "lambda_tools" block in infra/terraform/main.tf, mostly from module "data".
#
# Step by step
#   1. Receive the zip location (bucket, key, version id) from module "data".
#   2. Receive the orders and returns table names (for the code) and ARNs (for the permissions).
#
# Key terms
#   ARN (Amazon Resource Name): the unique id of an AWS resource, used in permission rules.
# =====================================================================================================

# Naming, and where the code zip is stored in S3 (bucket, key and exact version).
variable "name_prefix" { type = string }
variable "bucket" { type = string }
variable "zip_key" { type = string }
variable "zip_version_id" { type = string }
# The orders and returns tables: names go into the function's settings, ARNs into its permissions.
variable "orders_table_name" { type = string }
variable "orders_table_arn" { type = string }
variable "returns_table_name" { type = string }
variable "returns_table_arn" { type = string }
# How many days CloudWatch keeps the function's logs.
variable "log_retention_days" { type = number }
