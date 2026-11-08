# =====================================================================================================
# modules/role_mapper/main.tf: the Lambda that turns Entra app roles into ShopAssist groups
#
# What this file does
#   Creates the function <prefix>-role-mapper from its container image, with its own minimal role (logs
#   only) and log group. The identity module attaches it to the Cognito user pool as the
#   pre-token-generation trigger (identity/role_mapper/handler.py explains the mapping).
#
# Where it fits
#   Created by the root main.tf only when enable_entra = true. Cognito calls it at every sign-in and
#   token refresh; the Runtime and Cedar then see Group-ShopAssist-* in `cognito:groups`.
#
# Step by step
#   1. An IAM role only the Lambda service may assume, capped by the environment's permissions boundary.
#   2. Permission to write its own logs, nothing else (it reads only the event Cognito sends).
#   3. A log group with short retention.
#   4. The function: container image, arm64, 5-second timeout, the mapping as environment variables.
#
# Key terms
#   Pre-token-generation trigger: a Lambda Cognito runs just before it issues tokens.
#   Permissions boundary: the ceiling on what a role may do, set by the platform stack.
# =====================================================================================================

locals {
  function_name = "${var.name_prefix}-role-mapper"
}

# Step 1: who may use the role (the Lambda service, for this account only).
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name                 = "${local.function_name}-role"
  assume_role_policy   = data.aws_iam_policy_document.assume.json
  permissions_boundary = var.permissions_boundary_arn
}

# Step 2: write its own log stream; that's all it needs.
data "aws_iam_policy_document" "logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.this.arn}:*"]
  }
}

resource "aws_iam_role_policy" "this" {
  name   = "logs"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.logs.json
}

# Step 3: created by Terraform (not by Lambda) so it has a retention and goes away on destroy.
resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days
}

# Step 4: the function.
resource "aws_lambda_function" "this" {
  function_name = local.function_name
  role          = aws_iam_role.this.arn
  package_type  = "Image"
  image_uri     = var.image_uri
  architectures = ["arm64"]
  timeout       = 5 # Cognito waits at most 5 seconds for a trigger
  memory_size   = 128

  environment {
    variables = {
      ENTRA_PROVIDER_NAME = var.provider_name
      ENTRA_ROLE_SOURCE   = var.role_source
      ENTRA_ROLE_MAP      = jsonencode(var.role_map)
    }
  }

  depends_on = [aws_cloudwatch_log_group.this, aws_iam_role_policy.this]
}
