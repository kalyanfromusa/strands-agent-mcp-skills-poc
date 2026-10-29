# =====================================================================================================
# modules/mcp_runtime/main.tf: the `support` tools server on AgentCore Runtime
#
# What this file does
#   Runs mcp_servers/support_mcp/server.py (a FastMCP server with the tools create_ticket, get_ticket
#   and search_faq) on AgentCore Runtime, with its own permissions and log group.
#
# Where it fits
#   Called by the root main.tf (module "mcp_runtime") with the zip and tickets table from module "data"
#   and the Cognito details from module "identity". The Gateway's `support` target calls this server
#   (with a machine token) when the agent uses a support tool; the server reads and writes the
#   tickets table in DynamoDB.
#
# Step by step
#   1. Create an IAM role that only AgentCore, acting for this account, may use.
#   2. Allow it to read its code zip, use the tickets table, write logs/traces/metrics and get its
#      workload identity token.
#   3. Wait 15 seconds so AWS has spread the new permissions everywhere.
#   4. Create the runtime <prefix>_support_mcp from artifacts/support_mcp.zip, speaking MCP, and only
#      accepting tokens from the M2M client.
#   5. Create its log group with a short retention.
#
# Key terms
#   AgentCore Runtime: an AWS service that runs our Python code in small isolated virtual machines
#     (microVMs) and gives it an HTTPS address.
#   FastMCP: a Python library for writing MCP servers. MCP (Model Context Protocol) is a standard way
#     for an AI agent to discover and call tools.
#   IAM (Identity and Access Management) role: a set of AWS permissions a service is allowed to use.
#   JWT (JSON Web Token) authorizer: a gatekeeper that checks the login token on every request.
#   Workload identity: an identity AgentCore gives each runtime so it can get tokens for itself.
#   X-Ray: the AWS service that records traces (timelines of each request) for troubleshooting.
# =====================================================================================================

# runtime_name: the runtime's name, e.g. shopassist_support_mcp (underscores only, no dashes allowed).
# runtime_logs: the pattern that matches every AgentCore runtime log group in this account and region.
locals {
  runtime_name = "${var.name_prefix}_support_mcp" # runtime names: [a-zA-Z][a-zA-Z0-9_]{0,47}
  runtime_logs = "arn:aws:logs:${var.region}:${var.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"
}

# ---------------------------------------------------------------- Execution role

# Step 1a: the trust policy. Only the AgentCore service may assume this role, and only when it acts
# for a resource in this account. That stops another AWS customer from tricking AgentCore into using
# our role (the "confused deputy" problem).
# Confused-deputy guard: only AgentCore acting for *this* account may assume the role.
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock-agentcore:${var.region}:${var.account_id}:*"]
    }
  }
}

# Step 1b: the "execution role" the support server runs as.
resource "aws_iam_role" "this" {
  name               = "${var.name_prefix}-support-mcp-runtime-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

# Step 2a: what the support server may do:
#   ReadCodeZip:      download its own zip (and only that file) from the artifacts bucket.
#   Tickets:          read, create, update and delete items in the tickets table only.
#   Logs/XRay/Metrics: write its logs, request traces and metrics so you can see what it did.
#   WorkloadIdentity: fetch the token for its own workload identity.
data "aws_iam_policy_document" "permissions" {
  statement {
    sid       = "ReadCodeZip"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["arn:aws:s3:::${var.bucket}/${var.zip_key}"]
  }

  statement {
    sid = "Tickets"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:Scan",
    ]
    resources = [var.tickets_table_arn]
  }

  # Standard AgentCore Runtime observability permissions.
  statement {
    sid       = "LogGroups"
    actions   = ["logs:CreateLogGroup", "logs:DescribeLogStreams"]
    resources = [local.runtime_logs]
  }
  statement {
    sid       = "DescribeLogGroups"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["arn:aws:logs:${var.region}:${var.account_id}:log-group:*"]
  }
  statement {
    sid       = "LogEvents"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${local.runtime_logs}:log-stream:*"]
  }
  statement {
    sid       = "XRay"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
    resources = ["*"]
  }
  # Metrics are allowed only in the "bedrock-agentcore" namespace (the metrics folder in CloudWatch).
  statement {
    sid       = "Metrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["bedrock-agentcore"]
    }
  }
  # The runtime gets a workload identity automatically; the service fetches its
  # token with the execution role.
  statement {
    sid = "WorkloadIdentity"
    actions = [
      "bedrock-agentcore:GetWorkloadAccessToken",
      "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
      "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
    ]
    resources = [
      "arn:aws:bedrock-agentcore:${var.region}:${var.account_id}:workload-identity-directory/default",
      "arn:aws:bedrock-agentcore:${var.region}:${var.account_id}:workload-identity-directory/default/workload-identity/${local.runtime_name}-*",
    ]
  }
}

# Step 2b: attach those permissions to the role.
resource "aws_iam_role_policy" "this" {
  name   = "support-mcp-runtime"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.permissions.json
}

# Step 3: pause for 15 seconds after the permissions are attached (time_sleep is a resource whose
# only job is to wait). We wait because a new IAM role takes a few seconds to become usable
# everywhere in AWS, and creating the runtime too early fails. `triggers` makes it wait again
# whenever the permissions change.
# IAM is eventually consistent; CreateAgentRuntime validates the role right away.
resource "time_sleep" "iam" {
  create_duration = "15s"
  triggers        = { policy = aws_iam_role_policy.this.policy }
}

# ---------------------------------------------------------------- Runtime

# Step 4: the support server itself.
#   agent_runtime_artifact / code_configuration: "direct code deploy". AgentCore downloads the zip
#     from S3 and runs server.py with Python 3.12, so no Docker image is needed. Pinning version_id
#     means a rebuilt zip (new S3 version) is picked up by the next `terraform apply`.
#   protocol_configuration MCP: AgentCore forwards MCP requests to the server inside the microVM.
#   network_configuration PUBLIC: the runtime has internet access and no private network (VPC) setup.
#   authorizer_configuration: only tokens issued by our Cognito pool to the M2M client are accepted,
#     so only the Gateway (and local tests with M2M credentials) can call these tools directly.
#   environment_variables: settings the Python code reads at start-up (use DynamoDB, which table).
#   depends_on: start only after the 15-second IAM pause.
resource "aws_bedrockagentcore_agent_runtime" "this" {
  agent_runtime_name = local.runtime_name
  description        = "ShopAssist support tools (FastMCP, stateless streamable HTTP)"
  role_arn           = aws_iam_role.this.arn

  agent_runtime_artifact {
    code_configuration {
      entry_point = ["server.py"]
      runtime     = "PYTHON_3_12"
      code {
        s3 {
          bucket = var.bucket
          prefix = var.zip_key # the object key of the zip, despite the name
          # A new version id (new zip) is a diff => in-place runtime update.
          version_id = var.zip_version_id
        }
      }
    }
  }

  # The runtime proxies MCP to 0.0.0.0:8000/mcp inside the microVM.
  protocol_configuration {
    server_protocol = "MCP"
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  # Only the gateway calls this server, with an M2M token from the Cognito client.
  # Cognito access tokens carry client_id (no aud), hence allowed_clients.
  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url   = var.discovery_url
      allowed_clients = var.allowed_client_ids
    }
  }

  environment_variables = {
    STORE         = "dynamodb"
    TICKETS_TABLE = var.tickets_table_name
    AWS_REGION    = var.region
  }

  depends_on = [time_sleep.iam]
}

# Step 5: keep the runtime's log messages for log_retention_days, and remove them on destroy.
# AgentCore itself creates /aws/bedrock-agentcore/runtimes/<runtime_id>-DEFAULT as soon as the
# runtime starts, so Terraform must not "create" it (that races and fails with "already exists").
# Instead this placeholder resource runs scripts/log_group.py on the computer running Terraform:
# "ensure" creates the group if it is still missing and sets the retention; on terraform destroy,
# "delete" removes it so no logs are left behind. Destroy-time commands may only use values saved
# in this resource ("self"), which is why the name, region and folder are stored in input.
resource "terraform_data" "log_group" {
  input = {
    name      = "/aws/bedrock-agentcore/runtimes/${aws_bedrockagentcore_agent_runtime.this.agent_runtime_id}-DEFAULT"
    region    = var.region
    days      = var.log_retention_days
    repo_root = var.repo_root
  }

  provisioner "local-exec" {
    working_dir = self.input.repo_root
    command     = "uv run python scripts/log_group.py ensure --name ${self.input.name} --region ${self.input.region} --days ${self.input.days}"
  }

  provisioner "local-exec" {
    when        = destroy
    working_dir = self.input.repo_root
    command     = "uv run python scripts/log_group.py delete --name ${self.input.name} --region ${self.input.region}"
  }
}
