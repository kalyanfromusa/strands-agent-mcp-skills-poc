# =====================================================================================================
# ci/iam.tf: the role GitHub Actions assumes, with only the permissions the pipeline needs
#
# What this file does
#   Creates <prefix>-github-ci, an IAM role that only workflow runs of one GitHub repository on one
#   branch can assume (through the OIDC provider in oidc.tf), and gives it a least-privilege policy.
#
# Where it fits
#   .github/workflows/deploy.yml sets `role-to-assume: ${{ vars.AWS_ROLE_ARN }}` to this role's ARN
#   (output ci_role_arn). Every job that touches AWS assumes it for at most one hour.
#
# Step by step
#   1. Trust policy: AssumeRoleWithWebIdentity from the GitHub OIDC provider, only when the token's
#      audience is sts.amazonaws.com AND its subject is repo:<owner/name>:ref:refs/heads/<branch>.
#      Pull requests, other branches, forks and other repositories get "Not authorized".
#   2. Permissions, one statement per pipeline step:
#        EcrLogin          docker login to ECR (GetAuthorizationToken only works on "*").
#        EcrPushPull       push and inspect images in the two repositories only.
#        SkillsList/Write  `aws s3 sync skills/ s3://<bucket>/skills/ --delete`, nothing outside skills/.
#        Runtimes          read and update the two runtimes (and read their DEFAULT endpoint).
#        PassRuntimeRoles  UpdateAgentRuntime re-sends roleArn, which counts as passing the role, so
#                          the CI role may pass exactly the two runtime execution roles to AgentCore.
#        GatewayTargets    list/read the gateway's targets and re-synchronize the `support` target.
#   3. Attach the policy to the role.
#
# Key terms
#   Least privilege: grant only the actions and resources a job really uses.
#   sub (subject) claim: in a GitHub OIDC token, e.g. repo:owner/name:ref:refs/heads/main. Jobs that
#     use a GitHub `environment:` get repo:owner/name:environment:<env> instead, so the workflow uses none.
#   iam:PassRole: permission to hand a role to an AWS service so the service can act as that role.
# =====================================================================================================

data "aws_caller_identity" "current" {}

# Names and ARNs, derived from the main stack's naming rules unless exact ids were given.
locals {
  account_id = data.aws_caller_identity.current.account_id
  bucket     = var.artifacts_bucket != "" ? var.artifacts_bucket : "${var.name_prefix}-artifacts-${local.account_id}-${var.region}"
  ac_arn     = "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}"

  agent_runtime_id = var.agent_runtime_id != "" ? var.agent_runtime_id : "${var.name_prefix}_agent-*"
  mcp_runtime_id   = var.support_mcp_runtime_id != "" ? var.support_mcp_runtime_id : "${var.name_prefix}_support_mcp-*"
  gateway_id       = var.gateway_id != "" ? var.gateway_id : "${var.name_prefix}-gateway-*"

  # Runtime ARNs plus everything below them (runtime-endpoint/DEFAULT, versions).
  runtime_arns = flatten([
    for id in [local.agent_runtime_id, local.mcp_runtime_id] : ["${local.ac_arn}:runtime/${id}", "${local.ac_arn}:runtime/${id}/*"]
  ])
  gateway_arns = ["${local.ac_arn}:gateway/${local.gateway_id}", "${local.ac_arn}:gateway/${local.gateway_id}/*"]

  # Execution roles created by the main stack (modules agent_runtime / mcp_runtime, infra/boto3 iam.py).
  runtime_role_arns = [
    "arn:aws:iam::${local.account_id}:role/${var.name_prefix}-agent-runtime-role",
    "arn:aws:iam::${local.account_id}:role/${var.name_prefix}-support-mcp-runtime-role",
  ]
}

# ---------------------------------------------------------------- Step 1: trust

# Only this repository's workflow runs on this branch, with a token meant for AWS STS.
data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:ref:refs/heads/${var.github_branch}"]
    }
  }
}

resource "aws_iam_role" "ci" {
  name                 = "${var.name_prefix}-github-ci"
  description          = "GitHub Actions deploy role for ${var.github_repository}@${var.github_branch} (container images)"
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = 3600
}

# ---------------------------------------------------------------- Step 2: permissions

data "aws_iam_policy_document" "ci" {
  # docker login: the token call can't be scoped to a repository.
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  # buildx push (layer upload + manifest), the "does this image exist" check before a rollback, and
  # the pulls buildx does to compare layers.
  statement {
    sid = "EcrPushPull"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:DescribeImages",
    ]
    resources = [for r in aws_ecr_repository.this : r.arn]
  }

  # `aws s3 sync` lists the destination to decide what to upload or delete: list only under skills/.
  statement {
    sid       = "SkillsList"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${local.bucket}"]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = [var.skills_prefix, "${var.skills_prefix}*"]
    }
  }
  statement {
    sid       = "SkillsWrite"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${local.bucket}/${var.skills_prefix}*"]
  }

  # Read the current runtime settings, send them back with the new image, and watch DEFAULT go live.
  statement {
    sid = "Runtimes"
    actions = [
      "bedrock-agentcore:GetAgentRuntime",
      "bedrock-agentcore:UpdateAgentRuntime",
      "bedrock-agentcore:GetAgentRuntimeEndpoint",
    ]
    resources = local.runtime_arns
  }

  # UpdateAgentRuntime includes roleArn: allowed only for the two runtime roles, only towards AgentCore.
  statement {
    sid       = "PassRuntimeRoles"
    actions   = ["iam:PassRole"]
    resources = local.runtime_role_arns
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["bedrock-agentcore.amazonaws.com"]
    }
  }

  # Re-list the support MCP server's tools after its image changed.
  statement {
    sid = "GatewayTargets"
    actions = [
      "bedrock-agentcore:ListGatewayTargets",
      "bedrock-agentcore:GetGatewayTarget",
      "bedrock-agentcore:SynchronizeGatewayTargets",
    ]
    resources = local.gateway_arns
  }
}

# Step 3: attach it.
resource "aws_iam_role_policy" "ci" {
  name   = "github-ci-deploy"
  role   = aws_iam_role.ci.id
  policy = data.aws_iam_policy_document.ci.json
}
