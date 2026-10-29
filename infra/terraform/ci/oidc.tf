# =====================================================================================================
# ci/oidc.tf: let GitHub Actions log in to AWS without stored keys
#
# What this file does
#   Creates (or looks up) the account's IAM OIDC identity provider for GitHub Actions,
#   https://token.actions.githubusercontent.com, with audience sts.amazonaws.com.
#
# Where it fits
#   iam.tf trusts this provider in the CI role's trust policy. In the workflow,
#   aws-actions/configure-aws-credentials asks GitHub for a signed token (that is what the
#   `id-token: write` permission allows) and exchanges it with AWS STS for one-hour credentials.
#
# Step by step
#   1. If var.create_oidc_provider is true, create the provider.
#   2. Otherwise read the existing one (an account can only have one provider per URL).
#   3. Expose its ARN as local.oidc_provider_arn for iam.tf.
#
# Key terms
#   OIDC token: a short-lived signed JSON Web Token in which GitHub states which repository, branch
#     and workflow is running (the `sub` claim). AWS checks the signature and the claims.
#   Audience (aud): who the token is meant for; sts.amazonaws.com for AWS.
#   Thumbprint: not needed; AWS validates GitHub's certificate through its own trusted root CAs.
# =====================================================================================================

locals {
  github_oidc_url = "https://token.actions.githubusercontent.com"
}

# Step 1: create it.
resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url            = local.github_oidc_url
  client_id_list = ["sts.amazonaws.com"]
}

# Step 2: or reuse the one that already exists in the account.
data "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 0 : 1

  url = local.github_oidc_url
}

# Step 3: whichever of the two applies.
locals {
  oidc_provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.github[0].arn
}
