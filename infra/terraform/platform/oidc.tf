# =====================================================================================================
# platform/oidc.tf: let AWS trust GitHub Actions' short-lived identity tokens (no stored AWS keys)
#
# What this file does
#   Registers GitHub's token issuer (token.actions.githubusercontent.com) as an OpenID Connect (OIDC)
#   identity provider in the account, or looks up the existing one.
#
# Where it fits
#   Each GitHub job asks GitHub for a signed token that says which repository, branch or environment it
#   runs for. AWS STS checks the signature against this provider, and the trust policies in iam_*.tf
#   decide which role the job may become. The result is one-hour credentials and no AWS keys in GitHub.
#
# Step by step
#   1. If create_oidc_provider = true, create the provider with audience sts.amazonaws.com.
#   2. Otherwise, look up the provider that already exists (only one per URL is allowed per account).
#   3. Expose its ARN as local.oidc_provider_arn for the role trust policies.
#
# Key terms
#   OIDC (OpenID Connect): a standard for one system to vouch for an identity with a signed token.
#   Audience (aud): who a token is meant for; GitHub's official AWS action asks for sts.amazonaws.com.
#   Thumbprint: no longer needed; AWS validates GitHub's certificate chain itself.
# =====================================================================================================

locals {
  github_oidc_url = "https://token.actions.githubusercontent.com"
}

# Step 1: create the provider.
resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url            = local.github_oidc_url
  client_id_list = ["sts.amazonaws.com"]
}

# Step 2: or reuse an existing one.
data "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 0 : 1

  url = local.github_oidc_url
}

# Step 3: one ARN for the trust policies, whichever way it was obtained.
locals {
  oidc_provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.github[0].arn
}
