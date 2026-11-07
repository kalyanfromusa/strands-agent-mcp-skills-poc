# Settings of the dev environment (committed: no secrets here; secrets come from TF_VAR_* in the pipeline).
# image_tag is NOT set here: the pipeline passes -var image_tag=<git sha> on every plan/apply.
environment        = "dev"
region             = "us-east-1"
name_prefix        = "shopassist"
model_id           = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
log_retention_days = 7
seed_data          = true
enable_budget      = false
