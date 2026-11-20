# =====================================================================================================
# modules/role_mapper/main.tf: the Lambda that turns Entra app roles into ShopAssist groups
#
# What this file does
#   Creates the function <prefix>-role-mapper (e.g. shopassist-prod-role-mapper) from its container
#   image, with its own minimal role (logs only) and log group. The identity module attaches it to the
#   Cognito user pool as the pre-token-generation trigger (identity/role_mapper/handler.py explains
#   the mapping).
#
# Where it fits
#   Created by the root main.tf only when enable_entra = true (module "role_mapper" has count = 0
#   otherwise, so none of this exists). Cognito calls it at every sign-in and token refresh. For users
#   who came through Entra it translates what Entra sent (app roles, or group ids) into one
#   Group-ShopAssist-* value in `cognito:groups`, which is all the Runtime and Cedar look at. Cognito's
#   own users (alice, bob, carol) pass through unchanged.
#
# Step by step
#   1. An IAM role only the Lambda service may assume, capped by the environment's permissions boundary.
#   2. Permission to write its own logs, nothing else (it reads only the event Cognito sends).
#   3. A log group with short retention.
#   4. The function: container image, arm64, 5-second timeout, the mapping as environment variables.
#
# Key terms
#   Pre-token-generation trigger: a Lambda Cognito runs just before it issues tokens, able to change
#     the claims (fields) inside them.
#   Permissions boundary: the ceiling on what a role may do, set by the platform stack.
#   IAM (Identity and Access Management) role: a set of AWS permissions a service is allowed to use.
#   Allow-list: only the values listed in the mapping count; anything else is ignored ("fail closed").
# =====================================================================================================

# The function's name; also used for its role and its log group (/aws/lambda/<name>).
locals {
  function_name = "${var.name_prefix}-role-mapper"
}

# Step 1a: the trust policy, i.e. who may use the role: only the AWS Lambda service. (Who may call the
# function is a separate rule: modules/identity allows only this environment's Cognito pool.)
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# Step 1b: the role the function runs as. The permissions boundary from the platform stack is the
# ceiling: whatever policy is attached, the role can never do more.
resource "aws_iam_role" "this" {
  name                 = "${local.function_name}-role"
  assume_role_policy   = data.aws_iam_policy_document.assume.json
  permissions_boundary = var.permissions_boundary_arn
}

# Step 2a: write log lines into its own log group; that's all it needs (no database, no secrets).
data "aws_iam_policy_document" "logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.this.arn}:*"]
  }
}

# Step 2b: attach that permission to the role.
resource "aws_iam_role_policy" "this" {
  name   = "logs"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.logs.json
}

# Step 3: the log group. Created by Terraform (not by Lambda on first run) so it has a retention and
# goes away on destroy.
resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days
}

# Step 4: the function, from its container image (Python 3.12 Lambda base image, arm64), with 128 MB
# of memory. A new image_uri (new git sha) is an in-place update. Its settings (environment variables):
#   ENTRA_PROVIDER_NAME: the Cognito provider whose users are mapped ("EntraID"); others pass through.
#   ENTRA_ROLE_SOURCE:   "roles" or "groups", i.e. which attribute to read (see root variables.tf).
#   ENTRA_ROLE_MAP:      the allow-list mapping as JSON, e.g. {"AppRole-ShopAssist-Admin": "Group-ShopAssist-Admin"}.
# depends_on makes sure the log group and permission exist before Cognito can call it.
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
