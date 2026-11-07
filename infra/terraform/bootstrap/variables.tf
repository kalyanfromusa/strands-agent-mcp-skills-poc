# =====================================================================================================
# bootstrap/variables.tf: settings for the state-bucket stack
#
# What this file does
#   Declares the region and the name prefix. The defaults match every other ShopAssist stack.
#
# Where it fits
#   main.tf builds the bucket name from these. Change them only if you also change the backend
#   settings of the platform and main stacks (their *.backend.hcl files name this bucket).
#
# Step by step
#   1. Terraform loads these defaults (override them with -var or a terraform.tfvars file).
#   2. The validation rule stops early if the prefix would make an invalid bucket name.
#
# Key terms
#   Prefix: the first part of every ShopAssist resource name (shopassist).
# =====================================================================================================

variable "region" {
  description = "AWS region for the state bucket (same region as the stacks that use it)."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Project prefix; the bucket is <prefix>-tfstate-<account>-<region>."
  type        = string
  default     = "shopassist"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,20}$", var.name_prefix))
    error_message = "name_prefix must be 2-21 lowercase letters/digits, starting with a letter."
  }
}
