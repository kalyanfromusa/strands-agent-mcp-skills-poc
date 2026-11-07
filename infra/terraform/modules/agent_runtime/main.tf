# =====================================================================================================
# modules/agent_runtime/main.tf: the ShopAssist agent on AgentCore Runtime
#
# What this file does
#   Runs the generic Strands agent (agent/app.py, packaged as a container image) on AgentCore Runtime
#   as <prefix>_<env>_agent, with its
#   own permissions, its login check and its log group.
#
# Where it fits
#   Called last by the root main.tf (module "agent_runtime"), because it needs the Gateway URL.
#   scripts/invoke_agent.py logs a user in with Cognito and sends the question plus the user's token
#   here. The runtime checks the token (layer 1 of access control), then the agent code loads the
#   skills from S3, asks the Bedrock model what to do, and calls tools through the Gateway with the
#   same user token (where layer 2, the Cedar policies, applies).
#
# Step by step
#   1. Create an IAM role that only AgentCore, acting for this account, may use.
#   2. Allow it to call the Bedrock model, pull its image, read the skills, write logs/traces/metrics and
#      get its workload identity token.
#   3. Wait 15 seconds so AWS has spread the new permissions everywhere.
#   4. Create the runtime from the image <ECR repo>:<git sha>: plain HTTP protocol, a token check that
#      only lets in Group-ShopAssist-Admin / -Viewer users, and the settings the agent code reads at start-up.
#   5. Create its log group with a short retention.
#
# Key terms
#   AgentCore Runtime: an AWS service that runs our Python code in small isolated virtual machines
#     (microVMs) and gives it an HTTPS address.
#   Strands: the open-source Python framework the agent is built with.
#   Bedrock: the AWS service that hosts AI models such as Claude; "InvokeModel" means "ask the model".
#   Inference profile: a Bedrock model id starting with us. that spreads requests over US regions.
#   JWT (JSON Web Token) authorizer: a gatekeeper that checks the login token on every request.
#   Custom claim: an extra field in the token, here cognito:groups (the user's Cognito groups).
#   Token passthrough: the agent forwards the user's own token to the Gateway instead of its own.
#   SigV4: the usual AWS request signing with access keys; not accepted once a JWT authorizer is set.
# =====================================================================================================

# runtime_name: the runtime's name, e.g. shopassist_agent (underscores only, no dashes allowed).
# runtime_logs: the pattern that matches every AgentCore runtime log group in this account and region.
locals {
  # Runtime names allow [a-zA-Z][a-zA-Z0-9_]{0,47}, so shopassist-prod becomes shopassist_prod_agent.
  runtime_name = "${replace(var.name_prefix, "-", "_")}_agent"
  runtime_logs = "arn:aws:logs:${var.region}:${var.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"
}

# ---------------------------------------------------------------- Execution role

# Step 1a: the trust policy. Only the AgentCore service, acting for a resource in this account, may
# assume this role. That stops another AWS customer from tricking AgentCore into using our role
# (the "confused deputy" problem).
# Confused-deputy guard: only AgentCore acting for this account may assume the role.
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

# Step 1b: the "execution role" the agent runs as.
resource "aws_iam_role" "this" {
  name               = "${var.name_prefix}-agent-runtime-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json

  # The ceiling set by the platform stack: whatever the policy below says, the role can never do more.
  permissions_boundary = var.permissions_boundary_arn
}

# Step 2a: what the agent may do:
#   WorkloadIdentity:       get the token for its own workload identity.
#   InvokeModel:            ask the Bedrock model (the agent's "brain") for the next step.
#   EcrAuth/EcrPullImage:   pull its own container image from ECR.
#   ListSkills, ReadSkills: list and download the files under skills/ only.
#   Logs/XRay/Metrics:      write logs, request traces and metrics.
# Note there is no permission to call the tools directly: all tool calls go through the Gateway,
# which does its own checks.
# No credential-provider / Secrets Manager permissions: the agent forwards the
# caller's own token to the gateway (passthrough, design.md section 8). Only the
# workload-identity token APIs remain (see the WorkloadIdentity statement).
data "aws_iam_policy_document" "permissions" {
  # With a JWT inbound authorizer the runtime exchanges the caller's JWT for a
  # workload access token (WorkloadAccessToken header) with the execution role.
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

  # A us.* inference profile routes to foundation models in several regions,
  # so both the profile and the regional model ARNs must be allowed.
  statement {
    sid     = "InvokeModel"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = [
      "arn:aws:bedrock:*::foundation-model/*",
      "arn:aws:bedrock:*:${var.account_id}:inference-profile/*",
    ]
  }

  # Pull its own container image from ECR (GetAuthorizationToken only works on "*").
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid       = "EcrPullImage"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    resources = [var.ecr_repository_arn]
  }

  # Cold-start sync of s3://bucket/skills/ to /tmp/skills.
  statement {
    sid       = "ListSkills"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.bucket}"]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${var.skills_prefix}*"]
    }
  }
  statement {
    sid       = "ReadSkills"
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${var.bucket}/${var.skills_prefix}*"]
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
}

# Step 2b: attach those permissions to the role.
resource "aws_iam_role_policy" "this" {
  name   = "agent-runtime"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.permissions.json
}

# Step 3: pause for 15 seconds after the permissions are attached. We wait because a new IAM role
# takes a few seconds to become usable everywhere in AWS, and creating the runtime too early fails.
# `triggers` makes it wait again whenever the permissions change.
# IAM is eventually consistent; CreateAgentRuntime validates the role right away.
resource "time_sleep" "iam" {
  create_duration = "15s"
  triggers        = { policy = aws_iam_role_policy.this.policy }
}

# ---------------------------------------------------------------- Runtime

# Step 4: the agent itself.
#   agent_runtime_artifact / container_configuration: AgentCore pulls <ECR repo>:<git sha> and runs it
#     (CMD python app.py, port 8080). A new image_tag is a diff => an in-place runtime update, which is
#     how the pipeline rolls out a new version (and rolls back to the previous one).
#   protocol_configuration HTTP: the agent is a plain web service (one request in, one reply out).
#   network_configuration PUBLIC: the runtime has internet access and no private network (VPC) setup.
#   authorizer_configuration: the login check described below (layer 1 of access control).
#   request_header_configuration: which request headers are passed through to the agent code.
#   environment_variables: the settings the agent code reads at start-up (see agent/config.py).
#   depends_on: start only after the 15-second IAM pause.
resource "aws_bedrockagentcore_agent_runtime" "this" {
  agent_runtime_name = local.runtime_name
  description        = "Generic skills-driven Strands agent"
  role_arn           = aws_iam_role.this.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = var.image_uri
    }
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  # The token must come from our Cognito pool and the users client (allowed_clients), and its
  # cognito:groups field (custom_claim, a list of group names) must contain at least one of
  # allowed_groups (CONTAINS_ANY): Group-ShopAssist-Admin or Group-ShopAssist-Viewer. alice and bob pass; carol, who has no
  # group, gets "403 Forbidden".
  # Layer 1 of access control: the runtime rejects (403) tokens from other clients
  # or users outside the allowed groups, before any agent code runs (carol).
  # A runtime with a JWT authorizer no longer accepts SigV4 invocations.
  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url   = var.discovery_url
      allowed_clients = var.allowed_client_ids

      custom_claim {
        inbound_token_claim_name       = "cognito:groups"
        inbound_token_claim_value_type = "STRING_ARRAY"
        authorizing_claim_match_value {
          claim_match_operator = "CONTAINS_ANY"
          claim_match_value {
            match_value_string_list = var.allowed_groups
          }
        }
      }
    }
  }

  # By default AgentCore does not pass the caller's Authorization header (which holds the token) on
  # to the code. Allowing it here is what makes token passthrough possible.
  # Lets the Authorization header reach the agent code so it can forward the
  # user's token to the gateway (GATEWAY_AUTH_MODE=passthrough).
  request_header_configuration {
    request_header_allowlist = ["Authorization"]
  }

  # MODEL_ID: which Bedrock model to use. GATEWAY_URL: where the tools are.
  # GATEWAY_AUTH_MODE passthrough: forward the user's token to the Gateway, so the Cedar policies
  #   judge the real user. OAUTH_SCOPES only matters in the agent's client_credentials mode (where it
  #   logs in as itself), and no current mode uses OAUTH_PROVIDER_NAME, so in passthrough mode both
  #   are informational.
  # SKILLS_S3_URI: where the agent downloads its skills from when it starts.
  environment_variables = {
    MODEL_ID            = var.model_id
    AWS_REGION          = var.region
    GATEWAY_URL         = var.gateway_url
    GATEWAY_AUTH_MODE   = "passthrough"
    OAUTH_PROVIDER_NAME = var.oauth_provider_name
    OAUTH_SCOPES        = join(" ", var.oauth_scopes)
    SKILLS_S3_URI       = var.skills_s3_uri
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
