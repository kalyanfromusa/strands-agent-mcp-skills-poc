# =====================================================================================================
# modules/lambda_tools/main.tf: the `orders` tools as an AWS Lambda function
#
# What this file does
#   Creates the Lambda function <prefix>-orders-tools that runs the orders tools (get_order,
#   list_customer_orders, check_return_eligibility, create_return), plus its permissions and log group.
#
# Where it fits
#   Called by the root main.tf (module "lambda_tools") with the zip location and table names from
#   module "data". The Gateway's `orders` target calls this Lambda when the agent uses an orders tool;
#   the Lambda then reads and writes the orders and returns tables in DynamoDB.
#
# Step by step
#   1. Create an IAM role that only the Lambda service may use.
#   2. Allow that role to read/write the orders and returns tables and to write its own logs.
#   3. Create the log group /aws/lambda/<prefix>-orders-tools with a short retention.
#   4. Create the Lambda function from artifacts/orders_lambda.zip, pinned to the uploaded version.
#
# Key terms
#   Lambda: an AWS service that runs a piece of code on demand; you pay only while it runs.
#   IAM (Identity and Access Management) role: a set of AWS permissions a service is allowed to use.
#   Assume role: the act of a service "putting on" a role to get its permissions.
#   Log group: a CloudWatch folder that collects a program's log messages.
#   arm64: the processor type (AWS Graviton); cheaper than x86 for the same work.
# =====================================================================================================

locals {
  function_name = "${var.name_prefix}-orders-tools"
}

# Step 1a: the "trust policy": only the AWS Lambda service may assume (use) this role.
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# Step 1b: the role the Lambda runs as. On its own it can do nothing; permissions are added below.
resource "aws_iam_role" "this" {
  name               = "${local.function_name}-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

# Step 2a: what the Lambda may do, and nothing more ("least privilege"):
#   - read, add and update items in the orders and returns tables, and look up orders by customer
#     through the customer_id-index;
#   - write log lines into its own log group only.
data "aws_iam_policy_document" "permissions" {
  statement {
    sid = "OrdersAndReturns"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
      "dynamodb:Scan",
    ]
    resources = [
      var.orders_table_arn,
      "${var.orders_table_arn}/index/*", # customer_id-index
      var.returns_table_arn,
    ]
  }

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.this.arn}:*"]
  }
}

# Step 2b: attach those permissions to the role.
resource "aws_iam_role_policy" "this" {
  name   = "orders-tools"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.permissions.json
}

# Step 3: the log group. Its name follows the Lambda convention /aws/lambda/<function name>, so
# Lambda writes into it automatically.
# Created by Terraform (not by Lambda on first run) so it has a retention and is
# removed by `terraform destroy`.
resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days
}

# Step 4: the function itself. Python 3.12 on arm64, entry point lambda_handler in handler.py,
# at most 30 seconds and 256 MB of memory per call. The environment variables tell the code to use
# DynamoDB (not the in-memory store used for local runs, STORE=memory) and which tables to use. depends_on makes sure
# the log group and permissions exist before the first call can happen.
resource "aws_lambda_function" "this" {
  function_name = local.function_name
  role          = aws_iam_role.this.arn
  runtime       = "python3.12"
  architectures = ["arm64"]
  handler       = "handler.lambda_handler"
  timeout       = 30
  memory_size   = 256

  # Pinning the S3 object version is what makes a rebuilt zip redeploy.
  s3_bucket         = var.bucket
  s3_key            = var.zip_key
  s3_object_version = var.zip_version_id

  environment {
    variables = {
      STORE         = "dynamodb"
      ORDERS_TABLE  = var.orders_table_name
      RETURNS_TABLE = var.returns_table_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.this, aws_iam_role_policy.this]
}
