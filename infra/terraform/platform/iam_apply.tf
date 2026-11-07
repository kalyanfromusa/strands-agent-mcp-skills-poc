# =====================================================================================================
# platform/iam_apply.tf: the per-environment roles that run `terraform apply` for the main stack
#
# What this file does
#   For each environment (dev, prod) it creates:
#     - shopassist-<env>-boundary: a permissions boundary, the MAXIMUM any role created by the main
#       stack may ever do, whatever policy Terraform attaches to it;
#     - shopassist-gha-apply-<env>: the role GitHub Actions uses inside the GitHub environment <env>
#       (prod requires your approval) to create, change and delete that environment's resources.
#
# Where it fits
#   deploy.yml's apply job runs with `environment: <env>`, so GitHub's token says
#   repo:<repo>:environment:<env>, and only that matches the trust policy (iam_ci.tf step 2).
#   The apply role can only touch resources named shopassist-<env>-* / shopassist_<env>_* (or tagged
#   project=shopassist + environment=<env> where AWS names resources by generated id). This account
#   also hosts other projects, so this scoping is what keeps a ShopAssist deploy away from them.
#
# Step by step
#   1. Names and ARN patterns per environment.
#   2. The permissions boundary (a managed policy). The apply role may create roles only if they carry
#      this boundary, so it can't create a role more powerful than the boundary (privilege escalation).
#   3. The apply role's permissions, one statement group per AWS service the main stack uses.
#   4. The role itself and the policy attachment.
#
# Key terms
#   Permissions boundary: a ceiling on a role's permissions. Effective rights = policy AND boundary.
#   iam:PassRole: handing a role to an AWS service (for example "AgentCore, run my agent as this role").
#   ABAC (attribute-based access control): allowing an action because of a resource's tags, used for
#     Cognito pools, whose ARNs contain a generated id instead of a name.
#   Known to be broad: bedrock-agentcore actions are allowed on every AgentCore resource in this account
#     and region (several of its ARNs embed generated ids). Tighten once the deployed ARNs are known
#     (task 6.11 checks the negative cases).
# =====================================================================================================

# Step 1: names and ARN patterns per environment.
locals {
  arn_prefix = {
    ac     = "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}"
    iam    = "arn:aws:iam::${local.account_id}"
    logs   = "arn:aws:logs:${var.region}:${local.account_id}"
    lambda = "arn:aws:lambda:${var.region}:${local.account_id}"
    ddb    = "arn:aws:dynamodb:${var.region}:${local.account_id}"
    cog    = "arn:aws:cognito-idp:${var.region}:${local.account_id}"
    sm     = "arn:aws:secretsmanager:${var.region}:${local.account_id}"
  }

  env = {
    for e in var.environments : e => {
      dash  = "${var.name_prefix}-${e}" # shopassist-prod
      under = "${var.name_prefix}_${e}" # shopassist_prod (AgentCore names allow no hyphens)
    }
  }
}

# ---------------------------------------------------------------- Step 2: permissions boundary

data "aws_iam_policy_document" "boundary" {
  for_each = local.env
  #checkov:skip=CKV_AWS_111:A boundary is a ceiling, not a grant; "*" covers AgentCore, logs and ECR login, whose ARNs are generated.
  #checkov:skip=CKV_AWS_356:Same reason; data access (DynamoDB, S3, Lambda, secrets) is scoped to this environment.

  # The model calls of the agent.
  statement {
    sid       = "Bedrock"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ConverseStream"]
    resources = ["arn:aws:bedrock:*::foundation-model/*", "arn:aws:bedrock:*:${local.account_id}:inference-profile/*"]
  }
  # AgentCore runtime/gateway/identity calls made by the workload roles.
  statement {
    sid       = "AgentCore"
    actions   = ["bedrock-agentcore:*"]
    resources = ["*"]
  }
  # Data: only this environment's tables and artifacts bucket.
  statement {
    sid       = "DynamoDB"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem", "dynamodb:DescribeTable"]
    resources = ["${local.arn_prefix.ddb}:table/${each.value.dash}-*"]
  }
  statement {
    sid       = "S3"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = ["arn:aws:s3:::${each.value.dash}-artifacts-*", "arn:aws:s3:::${each.value.dash}-artifacts-*/*"]
  }
  # The Gateway invokes this environment's Lambdas only.
  statement {
    sid       = "InvokeOwnLambdas"
    actions   = ["lambda:InvokeFunction"]
    resources = ["${local.arn_prefix.lambda}:function:${each.value.dash}-*"]
  }
  # The Gateway reads the OAuth client secrets that AgentCore Identity stores for it.
  statement {
    sid       = "IdentitySecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["${local.arn_prefix.sm}:secret:bedrock-agentcore-identity!*"]
  }
  # Pull this project's images.
  statement {
    sid       = "EcrPull"
    actions   = ["ecr:GetAuthorizationToken", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    resources = ["*"]
  }
  # Logs, traces, metrics.
  statement {
    sid       = "Observability"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogGroups", "logs:DescribeLogStreams", "xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets", "cloudwatch:PutMetricData"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "boundary" {
  for_each = local.env

  name        = "${each.value.dash}-boundary"
  description = "Permissions boundary: the most any ShopAssist ${each.key} workload role may do"
  policy      = data.aws_iam_policy_document.boundary[each.key].json
}

# ---------------------------------------------------------------- Step 3: apply permissions

data "aws_iam_policy_document" "apply" {
  for_each = local.env
  #checkov:skip=CKV_AWS_111:Remaining "*" resources are create/list calls without a resource ARN, guarded by tag or service conditions.
  #checkov:skip=CKV_AWS_356:Same reason (CreateUserPool with RequestTag, CreateServiceLinkedRole for AgentCore, Describe/List calls).

  # 3a. Terraform state: this environment's state + lock file; read the platform outputs.
  statement {
    sid       = "StateList"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${local.state_bucket}"]
  }
  statement {
    sid       = "StateRead"
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${local.state_bucket}/platform.tfstate", "arn:aws:s3:::${local.state_bucket}/main/${each.key}.tfstate*"]
  }
  statement {
    sid       = "StateWrite"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${local.state_bucket}/main/${each.key}.tfstate", "arn:aws:s3:::${local.state_bucket}/main/${each.key}.tfstate.tflock"]
  }

  # 3b. Images: only the deploy job pushes images (right before deploying them), checks that tags
  #     exist, and deletes an image it pushed if the deploy then fails (keeps ECR at current + previous).
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid       = "EcrRepos"
    actions   = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages", "ecr:BatchDeleteImage", "ecr:DescribeRepositories", "ecr:GetRepositoryPolicy", "ecr:SetRepositoryPolicy"]
    resources = local.repo_arns
  }

  # 3c. AgentCore: runtimes, gateway, targets, policy engine, credential providers, workload identities.
  statement {
    sid       = "AgentCore"
    actions   = ["bedrock-agentcore:*"]
    resources = ["${local.arn_prefix.ac}:*"]
  }
  # Credential providers store client secrets in Secrets Manager under a reserved name prefix.
  statement {
    sid       = "AgentCoreIdentitySecrets"
    actions   = ["secretsmanager:CreateSecret", "secretsmanager:DeleteSecret", "secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue", "secretsmanager:UpdateSecret", "secretsmanager:TagResource"]
    resources = ["${local.arn_prefix.sm}:secret:bedrock-agentcore-identity!*"]
  }

  # 3d. DynamoDB tables of this environment (including the seed step's writes).
  statement {
    sid       = "DynamoDB"
    actions   = ["dynamodb:*"]
    resources = ["${local.arn_prefix.ddb}:table/${each.value.dash}-*"]
  }
  statement {
    sid       = "DynamoDBList"
    actions   = ["dynamodb:ListTables", "dynamodb:DescribeLimits"]
    resources = ["*"]
  }

  # 3e. Lambda functions of this environment (orders tools, role mapper).
  statement {
    sid       = "Lambda"
    actions   = ["lambda:*"]
    resources = ["${local.arn_prefix.lambda}:function:${each.value.dash}-*"]
  }

  # 3f. S3: this environment's artifacts bucket (skills files).
  statement {
    sid       = "ArtifactsBucket"
    actions   = ["s3:*"]
    resources = ["arn:aws:s3:::${each.value.dash}-artifacts-*", "arn:aws:s3:::${each.value.dash}-artifacts-*/*"]
  }
  statement {
    sid       = "S3List"
    actions   = ["s3:ListAllMyBuckets", "s3:GetBucketLocation"]
    resources = ["*"]
  }

  # 3g. Cognito: pools have generated ids, so access is decided by tags (ABAC).
  statement {
    sid       = "CognitoCreateTagged"
    actions   = ["cognito-idp:CreateUserPool", "cognito-idp:TagResource"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/project"
      values   = [var.name_prefix]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/environment"
      values   = [each.key]
    }
  }
  statement {
    sid       = "CognitoOwnPools"
    actions   = ["cognito-idp:*"]
    resources = ["${local.arn_prefix.cog}:userpool/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/project"
      values   = [var.name_prefix]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/environment"
      values   = [each.key]
    }
  }
  statement {
    sid       = "CognitoReadAndDomains"
    actions   = ["cognito-idp:ListUserPools", "cognito-idp:DescribeUserPoolDomain", "cognito-idp:CreateUserPoolDomain", "cognito-idp:DeleteUserPoolDomain", "cognito-idp:UpdateUserPoolDomain"]
    resources = ["*"]
  }

  # 3h. IAM: only roles named shopassist-<env>-*, only with the boundary, passed only to our services.
  statement {
    sid       = "CreateRolesWithBoundary"
    actions   = ["iam:CreateRole", "iam:PutRolePermissionsBoundary"]
    resources = ["${local.arn_prefix.iam}:role/${each.value.dash}-*"]
    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      values   = [aws_iam_policy.boundary[each.key].arn]
    }
  }
  statement {
    sid = "ManageOwnRoles"
    actions = [
      "iam:GetRole", "iam:DeleteRole", "iam:UpdateRole", "iam:UpdateAssumeRolePolicy", "iam:TagRole", "iam:UntagRole",
      "iam:ListRolePolicies", "iam:GetRolePolicy", "iam:PutRolePolicy", "iam:DeleteRolePolicy",
      "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole",
    ]
    resources = ["${local.arn_prefix.iam}:role/${each.value.dash}-*"]
  }
  statement {
    sid       = "PassOwnRoles"
    actions   = ["iam:PassRole"]
    resources = ["${local.arn_prefix.iam}:role/${each.value.dash}-*"]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["bedrock-agentcore.amazonaws.com", "lambda.amazonaws.com"]
    }
  }
  statement {
    sid       = "ReadBoundary"
    actions   = ["iam:GetPolicy", "iam:GetPolicyVersion"]
    resources = [aws_iam_policy.boundary[each.key].arn]
  }
  # AgentCore may need its service-linked role the first time (created once per account).
  statement {
    sid       = "ServiceLinkedRole"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["*"]
    condition {
      test     = "StringLike"
      variable = "iam:AWSServiceName"
      values   = ["*bedrock-agentcore.amazonaws.com"]
    }
  }
  # Never remove or change a boundary.
  statement {
    sid       = "NoBoundaryRemoval"
    effect    = "Deny"
    actions   = ["iam:DeleteRolePermissionsBoundary", "iam:CreatePolicyVersion", "iam:DeletePolicy", "iam:SetDefaultPolicyVersion"]
    resources = ["*"]
  }

  # 3i. CloudWatch Logs groups of this environment's Lambdas and runtimes.
  statement {
    sid     = "LogGroups"
    actions = ["logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:DeleteRetentionPolicy", "logs:TagResource", "logs:UntagResource", "logs:ListTagsForResource", "logs:TagLogGroup", "logs:ListTagsLogGroup"]
    resources = [
      "${local.arn_prefix.logs}:log-group:/aws/lambda/${each.value.dash}-*",
      "${local.arn_prefix.logs}:log-group:/aws/bedrock-agentcore/runtimes/${each.value.under}_*",
    ]
  }
  statement {
    sid       = "LogsDescribe"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["*"]
  }

  # 3j. The optional cost alert.
  statement {
    sid       = "Budget"
    actions   = ["budgets:ViewBudget", "budgets:ModifyBudget", "budgets:TagResource", "budgets:ListTagsForResource"]
    resources = ["arn:aws:budgets::${local.account_id}:budget/${each.value.dash}-*"]
  }

  # 3k. Identity and look-ups Terraform does on every run.
  statement {
    sid       = "Lookups"
    actions   = ["sts:GetCallerIdentity", "iam:ListOpenIDConnectProviders", "tag:GetResources"]
    resources = ["*"]
  }
}

# ---------------------------------------------------------------- Step 4: the roles

resource "aws_iam_role" "apply" {
  for_each = local.env

  name                 = "${var.name_prefix}-gha-apply-${each.key}"
  description          = "GitHub Actions: terraform apply for ${each.key} (GitHub environment ${each.key} only)"
  assume_role_policy   = data.aws_iam_policy_document.trust["apply-${each.key}"].json
  max_session_duration = 3600
}

resource "aws_iam_policy" "apply" {
  for_each = local.env

  name        = "${var.name_prefix}-gha-apply-${each.key}"
  description = "terraform apply for the ShopAssist ${each.key} main stack"
  policy      = data.aws_iam_policy_document.apply[each.key].json
}

resource "aws_iam_role_policy_attachment" "apply" {
  for_each = local.env

  role       = aws_iam_role.apply[each.key].name
  policy_arn = aws_iam_policy.apply[each.key].arn
}
