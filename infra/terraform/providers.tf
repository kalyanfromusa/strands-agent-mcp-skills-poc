# =====================================================================================================
# providers.tf: settings for the AWS provider (the plugin that creates AWS resources)
#
# What this file does
#   Tells the AWS provider which region to work in and adds three tags to every resource it creates.
#
# Where it fits
#   Every aws_* resource in this folder and in the modules uses this provider configuration. It logs
#   in with whatever AWS credentials the environment provides: in GitHub Actions, the short-lived
#   OIDC credentials of the plan or apply role (see ../platform); on a laptop, a profile such as
#   AWS_PROFILE=personal. No AWS keys are written in any file.
#
# Step by step
#   1. Use the region from var.region (default us-east-1) for every resource.
#   2. Add the tags project = <name_prefix> (default "shopassist"), environment = <dev|prod> and
#      managed-by = terraform to every resource that supports tags.
#
# Key terms
#   Region: the AWS data-centre area where resources live, e.g. us-east-1 (North Virginia).
#   Tag: a label (key = value) on an AWS resource, used to find, group and cost resources.
# =====================================================================================================

provider "aws" {
  region = var.region

  # Every taggable resource gets these tags without repeating them in each module. The leftover scan
  # after a destroy (scripts/leftovers.py) and Cost Explorer filters rely on them, and the apply role
  # only lets Terraform manage Cognito pools tagged with this project and environment (ABAC, see
  # ../platform/iam_apply.tf). (The optional budget in main.tf is account-wide, not tag-filtered.)
  default_tags {
    tags = {
      project     = var.name_prefix
      environment = var.environment
      managed-by  = "terraform"
    }
  }
}
