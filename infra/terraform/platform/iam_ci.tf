# =====================================================================================================
# platform/iam_ci.tf: the roles GitHub Actions can become, one per kind of job
#
# What this file does
#   Creates the CI roles, each assumable only by specific GitHub workflow runs (checked through the OIDC
#   provider in oidc.tf), each with only the permissions that job needs:
#
#     Role                         Who may assume it (OIDC "sub" claim)                 What it can do
#     shopassist-gha-plan          runs on main, and pull requests from this repo        read-only + Terraform state/lock
#     shopassist-gha-apply-<env>   jobs in GitHub environment <env> (prod: reviewer)     run `terraform apply` for <env>
#
#   The apply roles' permissions are in iam_apply.tf; this file holds the shared parts and the plan role.
#   There is deliberately no "build" role: images are pushed only by the deploy job (apply role), so
#   ECR only ever holds deployed images and can keep exactly two (current + previous), see ecr.tf.
#
# Where it fits
#   .github/workflows/*.yml use aws-actions/configure-aws-credentials with role-to-assume = one of these
#   ARNs (stored as GitHub variables). Forks never get an OIDC token, so a fork PR can't use any role.
#
# Step by step
#   1. Shared names: account id, state bucket, the four ECR repository ARNs.
#   2. A helper that builds a trust policy for a list of allowed "sub" values.
#   3. Plan role: AWS-managed ReadOnlyAccess, MINUS reads of business data (S3 objects outside the
#      ShopAssist buckets, DynamoDB items, secret values), PLUS state read and lock-file write.
#
# Key terms
#   sub (subject) claim: in GitHub's token, e.g. <prefix>:ref:refs/heads/main, <prefix>:pull_request, or
#     <prefix>:environment:prod (when the job uses an environment). This repository's prefix is the
#     immutable form repo:<owner>@<owner id>/<repo>@<repo id> (var.github_subject_prefix).
#   Trust policy: the part of a role that says WHO may become the role.
#   Permissions policy: the part that says WHAT the role may do.
#   Explicit deny: a "Deny" statement always wins over any "Allow", even one from a managed policy.
# =====================================================================================================

# Step 1: shared names.
data "aws_caller_identity" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  state_bucket = "${var.name_prefix}-tfstate-${local.account_id}-${var.region}"
  repo_arns    = [for r in aws_ecr_repository.this : r.arn]

  # GitHub's immutable subject format (see var.github_subject_prefix).
  sub_main         = "${var.github_subject_prefix}:ref:refs/heads/main"
  sub_pull_request = "${var.github_subject_prefix}:pull_request"
}

# Step 2: trust policies. One document per role, differing only in the allowed "sub" values.
data "aws_iam_policy_document" "trust" {
  for_each = merge(
    {
      plan = [local.sub_main, local.sub_pull_request]
    },
    { for env in var.environments : "apply-${env}" => ["${var.github_subject_prefix}:environment:${env}"] },
  )

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }
    # The token must be meant for AWS STS ...
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # ... and come from exactly the allowed repository + branch / PR / environment.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = each.value
    }
  }
}

# ---------------------------------------------------------------- Step 3: plan role

resource "aws_iam_role" "plan" {
  name                 = "${var.name_prefix}-gha-plan"
  description          = "GitHub Actions: terraform plan only (${var.github_repository}, main + same-repo PRs)"
  assume_role_policy   = data.aws_iam_policy_document.trust["plan"].json
  max_session_duration = 3600
}

# Read the configuration of every AWS resource (what `terraform plan` refreshes).
resource "aws_iam_role_policy_attachment" "plan_readonly" {
  role       = aws_iam_role.plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "plan" {
  # 3a: read every state file and the platform outputs; write/delete only the lock files.
  statement {
    sid       = "StateList"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${local.state_bucket}"]
  }
  statement {
    sid       = "StateRead"
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${local.state_bucket}/*"]
  }
  statement {
    sid       = "LockFiles"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${local.state_bucket}/*.tflock"]
  }

  # 3b: ReadOnlyAccess would also allow reading business data. Deny that explicitly.
  # S3 objects: only the state bucket and the ShopAssist artifact buckets (skills files) may be read.
  statement {
    sid     = "DenyOtherS3Objects"
    effect  = "Deny"
    actions = ["s3:GetObject", "s3:GetObjectVersion"]
    not_resources = [
      "arn:aws:s3:::${local.state_bucket}/*",
      "arn:aws:s3:::${var.name_prefix}-*-artifacts-${local.account_id}-${var.region}/*",
    ]
  }
  # Table items, secret values, Lambda code downloads and Cognito user lists are never needed for a plan.
  statement {
    sid    = "DenyDataReads"
    effect = "Deny"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:BatchGetItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:GetRecords",
      "dynamodb:ExportTableToPointInTime",
      "secretsmanager:GetSecretValue",
      "ssm:GetParameter*",
      "kms:Decrypt",
      "cognito-idp:ListUsers",
      "cognito-idp:AdminGetUser",
      "logs:GetLogEvents",
      "logs:FilterLogEvents",
      "logs:StartQuery",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "plan" {
  name   = "terraform-plan"
  role   = aws_iam_role.plan.id
  policy = data.aws_iam_policy_document.plan.json
}
