# =====================================================================================================
# modules/chat_relay/main.tf: the chat relay, a Lambda function with a public function URL
#
# What this file does
#   Creates the Lambda function <prefix>-chat-relay that the hosted chat page posts to. It checks the
#   request, forwards it with the user's own Cognito access token to the agent Runtime, and returns
#   the answer. It exists because the AgentCore Runtime does not answer browser CORS preflights.
#
# Where it fits
#   Called by the root main.tf (module "chat_relay") with its container image (same shape
#   as module "role_mapper"), the Runtime URL from module "agent_runtime" and the CloudFront address
#   from module "chat_site". The function URL is written into the page's config.json.
#
# Step by step
#   1. Create an IAM role that only the Lambda service may use, with the permissions boundary.
#   2. Allow it to write its own logs and nothing else (it calls the Runtime with the user's token,
#      not with AWS permissions).
#   3. Create the log group /aws/lambda/<prefix>-chat-relay with a short retention.
#   4. Create the Lambda function from the container image <ECR repo>:<git sha> (arm64), with a
#      reserved-concurrency cap.
#   5. Create the function URL (no AWS signature needed; the Runtime's JWT authorizer and the Cedar
#      policies decide who may do what) with CORS for the CloudFront origin only.
#   6. Allow the public to call the function URL.
#
# Key terms
#   Function URL: an HTTPS address of a Lambda function. authorization_type NONE means anyone can send
#     a request; the relay then refuses anything without a valid-looking token and the Runtime
#     rejects invalid ones.
#   CORS (cross-origin resource sharing): browser rules about which website may call which address.
#   Reserved concurrency: a cap on parallel runs; it limits cost if the address is flooded.
#   Permissions boundary: a ceiling on what the role may ever do, set by the platform stack.
# =====================================================================================================

# The function's name, e.g. shopassist-prod-chat-relay; also used for its role and log group.
locals {
  function_name = "${var.name_prefix}-chat-relay"
}

# Step 1a: the trust policy: only the AWS Lambda service may assume (use) this role.
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# Step 1b: the role the Lambda runs as, capped by the platform's permissions boundary.
resource "aws_iam_role" "this" {
  name                 = "${local.function_name}-role"
  assume_role_policy   = data.aws_iam_policy_document.assume.json
  permissions_boundary = var.permissions_boundary_arn
}

# Step 2: the only permission: write log lines into its own log group.
data "aws_iam_policy_document" "permissions" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.this.arn}:*"]
  }
}

resource "aws_iam_role_policy" "this" {
  name   = "chat-relay"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.permissions.json
}

# Step 3: the log group, created by Terraform so it has a retention and `terraform destroy` removes it.
resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days
}

# Step 4: the function. It may run up to 300 seconds (an agent answer can take a while) and the
# urllib call inside waits at most TIMEOUT_SECONDS (280), so the relay can answer with a clean 504
# before Lambda cuts it off. reserved_concurrent_executions is the spend cap (null = no reservation).
# The environment variables hold no secrets: the Runtime URL and the page origin.
resource "aws_lambda_function" "this" {
  function_name                  = local.function_name
  role                           = aws_iam_role.this.arn
  package_type                   = "Image"
  image_uri                      = var.image_uri
  architectures                  = ["arm64"]
  timeout                        = 300
  memory_size                    = 256
  reserved_concurrent_executions = var.reserved_concurrency

  environment {
    variables = {
      RUNTIME_URL     = var.runtime_url
      ALLOWED_ORIGIN  = var.allowed_origin
      TIMEOUT_SECONDS = "280"
    }
  }

  depends_on = [aws_cloudwatch_log_group.this, aws_iam_role_policy.this]
}

# Step 5: the public address. The cors block makes Lambda answer the browser's preflight and add the
# Access-Control-* headers (the handler adds none, so they never appear twice).
resource "aws_lambda_function_url" "this" {
  function_name      = aws_lambda_function.this.function_name
  authorization_type = "NONE"

  cors {
    allow_origins = [var.allowed_origin]
    allow_methods = ["POST"]
    allow_headers = ["authorization", "content-type", "x-amzn-bedrock-agentcore-runtime-session-id"]
    max_age       = 3600
  }
}

# Step 6: a function URL with authorization NONE needs two resource permissions for everyone ("*"):
# one to call the URL and one to invoke the function through it.
resource "aws_lambda_permission" "url" {
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.this.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "invoke" {
  statement_id             = "AllowPublicInvokeViaUrl"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.this.function_name
  principal                = "*"
  invoked_via_function_url = true
}
