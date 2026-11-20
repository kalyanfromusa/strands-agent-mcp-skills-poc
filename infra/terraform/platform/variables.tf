# =====================================================================================================
# platform/variables.tf: settings for the shared platform stack (ECR, GitHub OIDC, CI roles)
#
# What this file does
#   Declares the inputs: region, prefix, the GitHub repository, the environments that get an apply
#   role, whether to create the GitHub OIDC provider, and how many images ECR keeps.
#
# Where it fits
#   ecr.tf, oidc.tf and the iam_*.tf files read these as var.<name>. Defaults match the decisions in
#   .kiro/steering/decisions.md, so a plain `terraform apply` needs no tfvars file.
#
# Step by step
#   1. Terraform loads the defaults, then any terraform.tfvars / -var overrides.
#   2. The validation rules stop early on invalid values.
#
# Key terms
#   Environment: dev or prod. Each one gets its own apply role, usable only from the GitHub
#     environment with the same name.
#   OIDC provider: the account-level trust in GitHub's token issuer (one per account).
# =====================================================================================================

# The AWS region of the ECR repositories. It must be the region the main stacks deploy to, because the
# runtimes and Lambdas pull their images from here.
variable "region" {
  description = "AWS region of the ECR repositories and of the main stacks."
  type        = string
  default     = "us-east-1"
}

# The first part of every shared name (shopassist-agent, shopassist-gha-plan, ...). It must match the
# prefix used by ../bootstrap (the state bucket name) and by the main stack.
variable "name_prefix" {
  description = "Project prefix shared by every ShopAssist stack."
  type        = string
  default     = "shopassist"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,20}$", var.name_prefix))
    error_message = "name_prefix must be 2-21 lowercase letters/digits, starting with a letter."
  }
}

# The GitHub repository in readable owner/name form. Used only in role descriptions; the trust
# policies use github_subject_prefix below.
variable "github_repository" {
  description = "GitHub repository (owner/name) whose workflows may assume the CI roles."
  type        = string
  default     = "kalyanfromusa/strands-agent-mcp-skills-poc"
}

# The exact start of the identity GitHub writes into this repository's tokens. The trust policies in
# iam_ci.tf only accept tokens whose subject starts with it.
variable "github_subject_prefix" {
  description = <<-EOT
    Start of the OIDC "sub" claim GitHub puts in this repository's tokens. This repository uses GitHub's
    immutable subject format, repo:<owner>@<owner id>/<repo>@<repo id> (check it with
    `gh api repos/<owner>/<repo>/actions/oidc/customization/sub`). The numeric ids mean a renamed or
    re-created repository with the same name can never assume these roles.
  EOT
  type        = string
  default     = "repo:kalyanfromusa@28615456/strands-agent-mcp-skills-poc@1438682520"

  validation {
    condition     = can(regex("^repo:[^:]+$", var.github_subject_prefix))
    error_message = "github_subject_prefix must look like repo:<owner>@<id>/<repo>@<id> (no trailing colon)."
  }
}

# Which environments get a permissions boundary and an apply role. Both are defined, even though only
# prod is deployed today.
variable "environments" {
  description = "Environments that get an apply role (each trusted only for the GitHub environment of the same name)."
  type        = set(string)
  default     = ["dev", "prod"]

  validation {
    condition     = alltrue([for e in var.environments : contains(["dev", "prod"], e)])
    error_message = "environments may only contain \"dev\" and \"prod\"."
  }
}

# AWS allows only one GitHub OIDC provider per account. true creates it here; set false if another
# project in the account already created it (oidc.tf then looks it up instead).
variable "create_oidc_provider" {
  description = "true: create the GitHub OIDC provider (none exists in 438750401181 as of 2026-11-06). false: use the existing one."
  type        = bool
  default     = true
}

# How many images each ECR repository keeps before the oldest is deleted (see ecr.tf step 4).
variable "ecr_keep_last_images" {
  description = "Images kept per ECR repository: 2 = the current image + one previous image for rollback."
  type        = number
  default     = 2

  validation {
    # Fewer than 2 would make a rollback impossible.
    condition     = var.ecr_keep_last_images >= 2
    error_message = "ecr_keep_last_images must be at least 2 (current + previous)."
  }
}
