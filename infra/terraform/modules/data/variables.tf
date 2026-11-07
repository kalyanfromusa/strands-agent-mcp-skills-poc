# =====================================================================================================
# modules/data/variables.tf: inputs of the storage module
#
# What this file does
#   Declares what the root main.tf must pass in: naming, local file paths and the seed switch.
#
# Where it fits
#   Filled in by the module "data" block in infra/terraform/main.tf; read by modules/data/main.tf.
#
# Step by step
#   1. Terraform receives these values from the root module when it builds module "data".
#   2. main.tf in this folder uses them to name the bucket and tables and to find local files.
#
# Key terms
#   Absolute path: a full file location on your computer, e.g. E:/strands-skills-mcp-poc/skills.
# =====================================================================================================

# Used to build unique names: <name_prefix>-orders, <name_prefix>-artifacts-<account_id>-<region>.
variable "name_prefix" { type = string }
variable "region" { type = string }
variable "account_id" { type = string }

variable "repo_root" {
  description = "Absolute repo root; working directory for the seed script."
  type        = string
}

variable "skills_dir" {
  description = "Local skills/ folder, mirrored to s3://<bucket>/skills/."
  type        = string
}

variable "seed_file" {
  description = "data/seed.json; a change re-runs the seed script."
  type        = string
}

# true = load the demo orders after the tables exist (copied from the root variable seed_data).
variable "seed_data" { type = bool }
