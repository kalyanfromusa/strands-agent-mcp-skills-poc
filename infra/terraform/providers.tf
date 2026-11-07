# =====================================================================================================
# providers.tf: settings for the AWS provider (the plugin that creates AWS resources)
#
# What this file does
#   Tells the AWS provider which region to work in and adds one tag to every resource it creates.
#
# Where it fits
#   Every aws_* resource in this folder and in the modules uses this provider configuration. It logs
#   in with your normal AWS credentials (environment variables, ~/.aws profile or SSO session).
#
# Step by step
#   1. Use the region from var.region (default us-east-1) for every resource.
#   2. Add the tag project = <name_prefix> (default "shopassist") to every resource that supports tags.
#
# Key terms
#   Region: the AWS data-centre area where resources live, e.g. us-east-1 (North Virginia).
#   Tag: a label (key = value) on an AWS resource, used to find, group and cost resources.
# =====================================================================================================

provider "aws" {
  region = var.region

  # Every taggable resource gets project=<prefix> without repeating it in each module,
  # which is also what the "find my leftovers" queries and Cost Explorer filters rely on.
  # (The optional budget in main.tf is account-wide, not tag-filtered: see the note there.)
  default_tags {
    tags = {
      project     = var.name_prefix
      environment = var.environment
      managed-by  = "terraform"
    }
  }
}
