# =====================================================================================================
# variables.tf: the settings you can change without editing any other Terraform file
#
# What this file does
#   Declares the inputs of the deployment, each with a description, a type and a safe default.
#
# Where it fits
#   Values come from terraform.tfvars (copy terraform.tfvars.example), from `-var name=value` on the
#   command line, or from the defaults below. main.tf and providers.tf read them as var.<name>.
#
# Step by step
#   1. Terraform loads the defaults below.
#   2. It overrides them with anything in terraform.tfvars or on the command line.
#   3. It runs each `validation` rule and stops with the error_message if a value is not allowed.
#
# Key terms
#   Variable: a named input setting, like a form field for the deployment.
#   Validation: a check that stops Terraform early with a clear message if a value is wrong.
#   Model id: the name of the Bedrock AI model (or inference profile) that the agent uses to think.
#   Inference profile: a Bedrock id starting with us. that spreads requests over several US regions.
# =====================================================================================================

# Which AWS region to deploy into. AgentCore and the chosen model must both be available there.
variable "region" {
  description = "AWS region for every resource."
  type        = string
  default     = "us-east-1"
}

# The short name used at the start of every resource name (e.g. shopassist-orders) and as the
# project tag. Change it if you want two copies side by side in one account.
variable "name_prefix" {
  description = "Prefix for resource names and the project tag. Must stay lowercase letters (used in S3, Cognito domain and AgentCore runtime names)."
  type        = string
  default     = "shopassist"

  validation {
    # AgentCore runtime names allow [a-zA-Z0-9_], Cognito domains and S3 allow [a-z0-9-]:
    # lowercase letters/digits are the only safe intersection.
    condition     = can(regex("^[a-z][a-z0-9]{1,20}$", var.name_prefix))
    error_message = "name_prefix must be 2-21 lowercase letters/digits, starting with a letter."
  }
}

# Which Bedrock model the agent uses. Your account must have access to it in var.region.
variable "model_id" {
  description = "Bedrock model id or inference profile id used by the agent."
  type        = string
  default     = "us.anthropic.claude-haiku-5-5"
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

# How many days CloudWatch keeps the logs of the Lambda and the two runtimes. Short keeps cost low.
variable "log_retention_days" {
  description = "CloudWatch Logs retention for Lambda and runtime log groups."
  type        = number
  default     = 7
}

# Load the demo orders into DynamoDB after the tables are created. Set to false if `uv` is not
# installed on the machine running Terraform; you can then run scripts/seed_data.py by hand.
variable "seed_data" {
  description = "Run scripts/seed_data.py after the DynamoDB tables exist (needs uv on PATH)."
  type        = bool
  default     = true
}
