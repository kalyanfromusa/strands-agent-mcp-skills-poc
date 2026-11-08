# =====================================================================================================
# envs/prod.tfvars: the settings of the prod environment
#
# What this file does
#   Sets the main stack's variables for prod (see ../variables.tf for what each one means).
#
# Where it fits
#   Passed with -var-file=envs/prod.tfvars by the pipeline (ci.yml, deploy.yml, destroy.yml) or by
#   hand. Committed to git because it holds no secrets: secrets come from TF_VAR_* environment
#   variables (GitHub environment secrets). prod is the deployed environment.
#
# Step by step
#   1. Terraform loads the defaults in ../variables.tf.
#   2. The values below replace them.
#   3. image_tag is NOT set here: the pipeline passes -var image_tag=<git sha> on every plan/apply.
#   4. Entra ID sign-in is not switched on here (enable_entra stays false, its default).
#
# Key terms
#   tfvars file: a list of name = value settings for Terraform variables.
#   model_id: the Bedrock model (Claude Haiku 4.5 inference profile). Changing it is a one-line edit.
#   seed_data: load the demo orders after deploying. enable_budget: the optional cost alert (off).
# =====================================================================================================
environment        = "prod"
region             = "us-east-1"
name_prefix        = "shopassist"
model_id           = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
log_retention_days = 7
seed_data          = true
enable_budget      = false
