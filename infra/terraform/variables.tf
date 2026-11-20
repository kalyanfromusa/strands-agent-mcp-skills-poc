# =====================================================================================================
# variables.tf: the settings you can change without editing any other Terraform file
#
# What this file does
#   Declares the inputs of the deployment, each with a description, a type and a safe default.
#
# Where it fits
#   Values come from three places: the committed per-environment file envs/<env>.tfvars
#   (-var-file=envs/prod.tfvars), the command line (-var image_tag=<sha>, passed by the pipeline), and
#   TF_VAR_<name> environment variables for secrets (the GitHub environment secret for the Entra
#   client secret). Anything not set falls back to the defaults below. main.tf, outputs.tf and
#   providers.tf read them as var.<name>.
#
# Step by step
#   1. Terraform loads the defaults below.
#   2. It overrides them with envs/<env>.tfvars, TF_VAR_* environment variables and -var values.
#   3. It runs each `validation` rule and stops with the error_message if a value is not allowed.
#
# Key terms
#   Variable: a named input setting, like a form field for the deployment.
#   Validation: a check that stops Terraform early with a clear message if a value is wrong.
#   Model id: the name of the Bedrock AI model (or inference profile) that the agent uses to think.
#   Inference profile: a Bedrock id starting with us. that spreads requests over several US regions.
#   Entra ID: Microsoft's cloud directory (formerly Azure AD). Optionally federated into Cognito, so
#     company users can sign in with their Microsoft account (see the Entra section at the end).
# =====================================================================================================

# Which AWS region to deploy into. AgentCore and the chosen model must both be available there.
variable "region" {
  description = "AWS region for every resource."
  type        = string
  default     = "us-east-1"
}

# The short name used at the start of every resource name (e.g. shopassist-prod-orders) and as the
# project tag. It must match the prefix the platform and bootstrap stacks were created with, because
# the state bucket name and the ECR repositories are derived from it.
variable "name_prefix" {
  description = "Project prefix and project tag. Names are <prefix>-<environment>-*. Lowercase letters/digits only (used in S3, Cognito domain and AgentCore runtime names)."
  type        = string
  default     = "shopassist"

  validation {
    # AgentCore runtime names allow [a-zA-Z0-9_], Cognito domains and S3 allow [a-z0-9-]:
    # lowercase letters/digits are the only safe intersection.
    condition     = can(regex("^[a-z][a-z0-9]{1,15}$", var.name_prefix))
    error_message = "name_prefix must be 2-16 lowercase letters/digits, starting with a letter (room for -<environment>)."
  }
}

# Which environment this copy of the stack is (dev or prod). It becomes part of every resource name
# and selects the state file (envs/<env>.backend.hcl) and the settings (envs/<env>.tfvars).
variable "environment" {
  description = "Environment: dev or prod. Part of every resource name (shopassist-<env>-*)."
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be \"dev\" or \"prod\"."
  }
}

# The git commit SHA whose container images to run. The pipeline passes it (-var image_tag=<sha>);
# changing it is how a new version is rolled out, or an old one rolled back. It has no default on
# purpose: every plan must say exactly which code it deploys. The four images are the agent, the
# support MCP server, the orders Lambda and the role-mapper Lambda (all built from the same commit).
variable "image_tag" {
  description = "Full 40-character git commit SHA of the images in ECR (all four images share it)."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.image_tag))
    error_message = "image_tag must be a full 40-character lowercase hex git SHA."
  }
}

# Which Bedrock model the agent uses. Your account must have access to it in var.region.
variable "model_id" {
  description = "Bedrock model id or inference profile id used by the agent."
  type        = string
  default     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}

# Turn on the optional monthly cost alert in main.tf. Off by default so nothing emails you unasked.
variable "enable_budget" {
  description = "Create a monthly AWS Budget with an email alert."
  type        = bool
  default     = false
}

# Where the budget alerts go. Only needed (and then checked) when enable_budget = true.
variable "budget_email" {
  description = "Email address for budget alerts (required when enable_budget = true)."
  type        = string
  default     = ""

  validation {
    # Passes when the budget is off, or when the address looks like name@domain.tld.
    condition     = !var.enable_budget || can(regex("^[^@ ]+@[^@ ]+[.][^@ ]+$", var.budget_email))
    error_message = "Set budget_email to a valid address when enable_budget = true."
  }
}

# The monthly spending limit, in US dollars, that the budget alerts are measured against.
variable "budget_limit_usd" {
  description = "Monthly budget limit in USD."
  type        = number
  default     = 5
}

# How many days CloudWatch keeps the logs of the Lambdas (orders tools, and the role mapper when Entra
# is on) and of the two runtimes. Short keeps cost low.
variable "log_retention_days" {
  description = "CloudWatch Logs retention for Lambda and runtime log groups."
  type        = number
  default     = 7
}

# Load the demo orders into DynamoDB after the tables are created (the pipeline's runner has `uv`).
# Set to false if `uv` is not installed on the machine running Terraform; you can then run
# scripts/seed_data.py by hand.
variable "seed_data" {
  description = "Run scripts/seed_data.py after the DynamoDB tables exist (needs uv on PATH)."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------- Entra ID federation (P7)
# docs/12-entra-federation.md. Off by default (and off in envs/*.tfvars today); turn it on per
# environment in envs/<env>.tfvars once the Entra app registration exists. The client secret never
# goes in a file: the GitHub environment secret TF_VAR_ENTRA_CLIENT_SECRET is passed by deploy.yml as
# the environment variable TF_VAR_entra_client_secret. The validation rules below only bite when
# enable_entra = true, so with Entra off all of these may stay empty.

# The master switch. true adds the "Sign in with Entra" option to Cognito and creates the role-mapper
# Lambda; false (default) leaves only the Cognito demo users.
variable "enable_entra" {
  description = "Let users sign in with Entra ID (Cognito OIDC identity provider + role mapper)."
  type        = bool
  default     = false
}

# Which Entra directory (tenant) the users come from. It is part of the sign-in address Cognito trusts
# (https://login.microsoftonline.com/<tenant>/v2.0). Not secret.
variable "entra_tenant_id" {
  description = "Entra tenant id (GUID). Required when enable_entra = true."
  type        = string
  default     = ""

  validation {
    condition     = !var.enable_entra || can(regex("^[0-9a-fA-F-]{36}$", var.entra_tenant_id))
    error_message = "Set entra_tenant_id (a GUID) when enable_entra = true."
  }
}

# The id of the ShopAssist app registration in Entra (ShopAssist-Prod). Cognito presents it when it
# sends a user to Microsoft to sign in. Not secret.
variable "entra_client_id" {
  description = "Application (client) id of the Entra app registration (GUID). Required when enable_entra = true."
  type        = string
  default     = ""

  validation {
    condition     = !var.enable_entra || can(regex("^[0-9a-fA-F-]{36}$", var.entra_client_id))
    error_message = "Set entra_client_id (a GUID) when enable_entra = true."
  }
}

# The password of that app registration, which Cognito uses to prove itself to Microsoft.
# `sensitive` hides it from Terraform's screen output (it is still stored in the state file).
variable "entra_client_secret" {
  description = "Client secret of the Entra app registration. From the GitHub environment secret, never from a file."
  type        = string
  default     = ""
  sensitive   = true

  validation {
    condition     = !var.enable_entra || length(var.entra_client_secret) > 0
    error_message = "entra_client_secret is empty: set the GitHub prod environment secret TF_VAR_ENTRA_CLIENT_SECRET."
  }
}

# Where the role mapper finds a user's rights in what Entra sends:
#   roles  = the app roles (AppRole-ShopAssist-*) assigned to the user's AD groups. Assigning groups to
#            app roles needs an Entra ID P1/P2 licence (the P2 trial for this PoC).
#   groups = the user's group object ids, the fallback for the free Entra tier after the trial ends.
variable "entra_role_source" {
  description = "roles = Entra app roles (needs P1/P2); groups = group object ids (Entra Free fallback)."
  type        = string
  default     = "roles"

  validation {
    condition     = contains(["roles", "groups"], var.entra_role_source)
    error_message = "entra_role_source must be \"roles\" or \"groups\"."
  }
}

# The translation table, an allow-list: which Entra value becomes which ShopAssist Cognito group.
# Anything not listed is ignored, so an unknown role or group never grants access. In "groups" mode
# the keys must be replaced with the group object ids. The validation only allows the two groups the
# Runtime and the Cedar policies know.
variable "entra_role_map" {
  description = "Entra value (app role value, or group object id in groups mode) => ShopAssist Cognito group."
  type        = map(string)
  default = {
    "AppRole-ShopAssist-Admin"  = "Group-ShopAssist-Admin"
    "AppRole-ShopAssist-Viewer" = "Group-ShopAssist-Viewer"
  }

  validation {
    condition     = alltrue([for g in values(var.entra_role_map) : contains(["Group-ShopAssist-Admin", "Group-ShopAssist-Viewer"], g)])
    error_message = "entra_role_map values must be Group-ShopAssist-Admin or Group-ShopAssist-Viewer."
  }
}

# Which version of Cognito's "pre-token-generation" hook (the moment the role mapper runs) to use.
# V1_0 works on Cognito's free (Lite) tier; V2_0 can also edit access-token claims but needs the paid
# Essentials tier.
variable "entra_trigger_version" {
  description = "Cognito pre-token-generation trigger version (V1_0 works on the free tier)."
  type        = string
  default     = "V1_0"
}
