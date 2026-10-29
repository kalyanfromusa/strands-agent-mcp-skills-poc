# =====================================================================================================
# ci/variables.tf: the settings of the optional CI/CD stack
#
# What this file does
#   Declares every input of infra/terraform/ci, with defaults that match the main stack's naming
#   (design.md section 1), so `terraform apply` works with no tfvars file at all.
#
# Where it fits
#   Read by ecr.tf, oidc.tf and iam.tf. Copy terraform.tfvars.example to terraform.tfvars (git-ignored)
#   to override anything, for example to reuse an existing GitHub OIDC provider.
#
# Step by step
#   1. Where and how things are named (region, name_prefix).
#   2. Which GitHub repository and branch may assume the CI role.
#   3. Whether to create the GitHub OIDC provider or reuse the account's existing one.
#   4. Which runtimes, gateway and bucket the CI role may touch (derived from the prefix by default).
#   5. How many images ECR keeps.
#
# Key terms
#   OIDC (OpenID Connect) provider: tells AWS IAM to trust tokens that GitHub Actions issues, so the
#     workflow can get short-lived AWS credentials without any stored access keys.
#   Runtime id: <runtime name>-<random suffix>, e.g. shopassist_agent-AbC123xyz9.
# =====================================================================================================

# Step 1: naming.
variable "region" {
  description = "AWS region of the main stack (runtimes, gateway, bucket) and of the ECR repositories."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Same prefix as the main stack (infra/terraform var.name_prefix)."
  type        = string
  default     = "shopassist"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,20}$", var.name_prefix))
    error_message = "name_prefix must be 2-21 lowercase letters/digits, starting with a letter."
  }
}

# Step 2: who may assume the CI role.
variable "github_repository" {
  description = "GitHub repository as owner/name. Only workflows in this repository can assume the CI role."
  type        = string
  default     = "kalyanfromusa/strands-agent-mcp-skills-poc"
}

variable "github_branch" {
  description = "Only workflow runs on this branch can assume the CI role (OIDC sub = repo:<repo>:ref:refs/heads/<branch>)."
  type        = string
  default     = "main"
}

# Step 3: the OIDC provider. An account can have only ONE provider per URL, so if another project
# already created token.actions.githubusercontent.com, set create_oidc_provider = false to reuse it.
variable "create_oidc_provider" {
  description = "true: create the GitHub OIDC provider. false: look up the existing one in this account."
  type        = bool
  default     = true
}

# Step 4: what the CI role may touch. Empty strings mean "derive from name_prefix with a wildcard",
# which works before the main stack exists. Set the exact ids to tighten the policy further.
variable "agent_runtime_id" {
  description = "Exact agent runtime id (e.g. shopassist_agent-AbC123xyz9). Empty: <prefix>_agent-*."
  type        = string
  default     = ""
}

variable "support_mcp_runtime_id" {
  description = "Exact support MCP runtime id. Empty: <prefix>_support_mcp-*."
  type        = string
  default     = ""
}

variable "gateway_id" {
  description = "Exact gateway id (e.g. shopassist-gateway-abc123def4). Empty: <prefix>-gateway-*."
  type        = string
  default     = ""
}

variable "artifacts_bucket" {
  description = "Artifacts bucket of the main stack. Empty: <prefix>-artifacts-<account>-<region>."
  type        = string
  default     = ""
}

variable "skills_prefix" {
  description = "Key prefix of the skills folder in the artifacts bucket (main stack: skills/)."
  type        = string
  default     = "skills/"
}

# Step 5: image retention.
variable "ecr_keep_last_images" {
  description = "ECR keeps this many most recent images per repository and expires the rest."
  type        = number
  default     = 5
}
