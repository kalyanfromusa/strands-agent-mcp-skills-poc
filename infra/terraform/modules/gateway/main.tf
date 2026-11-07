# =====================================================================================================
# modules/gateway/main.tf: the single tools endpoint (AgentCore Gateway) and its access rules
#
# What this file does
#   Creates the AgentCore Gateway <prefix>-gateway: one MCP address that offers all ShopAssist tools,
#   with two "targets" behind it (`orders` = the Lambda, `support` = the support MCP server). It also
#   creates the policy engine and loads the Cedar policies that decide which caller may see and call
#   which tool.
#
# Where it fits
#   Called by the root main.tf (module "gateway") after lambda_tools, mcp_runtime and identity exist.
#   The agent sends every tool call here with the user's own login token. The Gateway checks the
#   token, asks the policy engine whether this user may use this tool, then forwards the call:
#     orders___<tool>  -> the orders Lambda (the Gateway uses its own IAM role)
#     support___<tool> -> the support MCP server (the Gateway gets an M2M token through the
#                         AgentCore Identity credential provider)
#   Its gateway_url output goes to module "agent_runtime".
#
# Step by step
#   1. Create the policy engine (an empty container for Cedar policies).
#   2. Create the Gateway's IAM role and its permissions, then wait 15 seconds for IAM to catch up.
#   3. Create the Gateway: token check (CUSTOM_JWT) + policy engine in ENFORCE mode.
#   4. Add the `orders` target (the Lambda, with its tool list from tools_schema.json).
#   5. Add the `support` target (the MCP server, reached with an OAuth machine token).
#   6. Load every infra/policies/*.cedar file into the policy engine, with placeholders filled in.
#
# Key terms
#   MCP (Model Context Protocol): a standard way for an AI agent to discover and call tools.
#   Target: one tool provider behind the Gateway. Tool names get the target as a prefix, e.g.
#     orders___get_order (three underscores).
#   Cedar: a small language for access rules ("permit this principal to do this action").
#   Policy engine: the AgentCore Policy component that stores Cedar policies and answers
#     "may this caller use this tool?" for the Gateway.
#   ENFORCE: the policy engine's answers are applied. Anything not permitted is denied, and
#     tools/list (the "what tools do you have?" request) only shows the tools the caller may use.
#   JWT (JSON Web Token): a signed login token that says who the caller is and which groups they are in.
#   IAM (Identity and Access Management) role: a set of AWS permissions a service is allowed to use.
# =====================================================================================================

# gateway_name / policy_engine_name: resource names (the policy engine allows letters, digits and _).
# arn_prefix: the common start of every AgentCore ARN in this account and region.
# tools: the orders tool definitions read from mcp_servers/orders_lambda/tools_schema.json.
# support_endpoint: the HTTPS address of the support MCP server on AgentCore Runtime.
locals {
  gateway_name       = "${var.name_prefix}-gateway"
  policy_engine_name = "${replace(var.name_prefix, "-", "_")}_policy_engine" # [A-Za-z][A-Za-z0-9_]*, max 48
  arn_prefix         = "arn:aws:bedrock-agentcore:${var.region}:${var.account_id}"

  tools = jsondecode(file(var.tools_schema_path))

  # Runtime invocation URL; the ARN must be URL-encoded because it contains ':' and '/'.
  support_endpoint = "https://bedrock-agentcore.${var.region}.amazonaws.com/runtimes/${urlencode(var.mcp_runtime_arn)}/invocations?qualifier=DEFAULT"
}

# ---------------------------------------------------------------- Policy engine

# Step 1: the policy engine, an empty container that the Cedar policies are added to in step 6.
# Created before the gateway: the gateway references it in policy_engine_configuration,
# and the gateway role must already be allowed to read it (else InternalServerException).
resource "aws_bedrockagentcore_policy_engine" "this" {
  name        = local.policy_engine_name
  description = "Cedar policies deciding which caller may list and call which ShopAssist tool"
}

# ---------------------------------------------------------------- Gateway role

# Step 2a: the trust policy. Only the AgentCore service, acting for a resource in this account, may
# assume the Gateway role. That stops another AWS customer from tricking AgentCore into using our
# role (the "confused deputy" problem).
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
      values   = ["${local.arn_prefix}:*"]
    }
  }
}

# Step 2b: the role the Gateway uses when it does work for a caller.
resource "aws_iam_role" "this" {
  name               = "${local.gateway_name}-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json

  # The ceiling set by the platform stack: the role can never do more than this boundary allows.
  permissions_boundary = var.permissions_boundary_arn
}

# Step 2c: what the Gateway may do:
#   InvokeOrdersLambda: call the orders Lambda (and no other function).
#   OutboundOAuth + ProviderSecret: get a machine token for the support server from AgentCore
#     Identity, which needs to read the stored M2M client secret.
#   PolicyEngine*: read the policy engine and ask it for allow/deny decisions on each request.
data "aws_iam_policy_document" "permissions" {
  # orders target: the gateway calls the Lambda with its own role (GATEWAY_IAM_ROLE).
  statement {
    sid       = "InvokeOrdersLambda"
    actions   = ["lambda:InvokeFunction"]
    resources = [var.lambda_arn]
  }

  # support target: the gateway's workload identity fetches an M2M token from the
  # credential provider (token vault), which reads the stored client secret.
  statement {
    sid = "OutboundOAuth"
    actions = [
      "bedrock-agentcore:GetWorkloadAccessToken",
      "bedrock-agentcore:GetResourceOauth2Token",
    ]
    resources = [
      "${local.arn_prefix}:workload-identity-directory/default",
      "${local.arn_prefix}:workload-identity-directory/default/workload-identity/*",
      "${local.arn_prefix}:token-vault/default",
      "${local.arn_prefix}:token-vault/default/*",
    ]
  }
  statement {
    sid       = "ProviderSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.oauth_provider_secret]
  }

  # Policy evaluation (devguide/policy-permissions.html). The gateway id is not known
  # when this role is created (it would be a cycle), so gateway/* within the account.
  statement {
    sid       = "PolicyEngineConfiguration"
    actions   = ["bedrock-agentcore:GetPolicyEngine"]
    resources = [aws_bedrockagentcore_policy_engine.this.policy_engine_arn]
  }
  statement {
    sid = "PolicyEngineAuthorization"
    actions = [
      "bedrock-agentcore:AuthorizeAction",
      "bedrock-agentcore:PartiallyAuthorizeActions", # filters tools/list per caller
    ]
    resources = [
      aws_bedrockagentcore_policy_engine.this.policy_engine_arn,
      "${local.arn_prefix}:gateway/*",
    ]
  }
}

# Step 2d: attach those permissions to the role.
resource "aws_iam_role_policy" "this" {
  name   = "gateway"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.permissions.json
}

# Step 2e: wait 15 seconds, because a new IAM role takes a few seconds to become usable everywhere
# in AWS. `triggers` makes it wait again whenever the permissions change.
# IAM is eventually consistent; CreateGateway validates the role right away.
resource "time_sleep" "iam" {
  create_duration = "15s"
  triggers        = { policy = aws_iam_role_policy.this.policy }
}

# ---------------------------------------------------------------- Gateway

# Step 3: the Gateway itself.
#   protocol_type MCP: callers talk to it with the standard MCP tool protocol.
#   authorizer CUSTOM_JWT: every request must carry a valid token from our Cognito pool, issued to
#     either the users client (tokens the agent forwards for alice/bob) or the M2M client (local
#     tests). Anything else is rejected before any tool runs.
#   policy_engine_configuration: every tools/list and tools/call is checked against the Cedar
#     policies. mode ENFORCE applies the decisions; LOG_ONLY would only record them (a dry run).
resource "aws_bedrockagentcore_gateway" "this" {
  name          = local.gateway_name
  description   = "Single MCP endpoint for the ShopAssist agent"
  role_arn      = aws_iam_role.this.arn
  protocol_type = "MCP"
  # Return target error details to the caller: handy while learning, drop in prod.
  exception_level = "DEBUG"

  authorizer_type = "CUSTOM_JWT"
  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url   = var.discovery_url
      allowed_clients = var.allowed_client_ids
    }
  }

  # No protocol_configuration block: the API rejects an empty MCP configuration, and leaving it
  # out keeps the defaults (no semantic tool search, which 7 tools do not need).

  # Default deny: without a matching permit, a tool is neither listed nor callable.
  policy_engine_configuration {
    arn  = aws_bedrockagentcore_policy_engine.this.policy_engine_arn
    mode = "ENFORCE" # or LOG_ONLY to dry-run the policies
  }

  depends_on = [time_sleep.iam]
}

# ---------------------------------------------------------------- Targets

# Step 4: the `orders` target. The Gateway calls the orders Lambda with its own IAM role
# (gateway_iam_role). The dynamic blocks turn each entry of tools_schema.json into one tool
# definition (name, description, and its input fields with type and "required" flag), so the
# Gateway can list the four orders tools to the agent.
resource "aws_bedrockagentcore_gateway_target" "orders" {
  gateway_identifier = aws_bedrockagentcore_gateway.this.gateway_id
  name               = "orders"
  description        = "Order lookup and returns (Lambda)"

  target_configuration {
    mcp {
      lambda {
        lambda_arn = var.lambda_arn

        # A Lambda has no tools/list, so the gateway needs the schemas up front.
        # Mapped from the JSON-Schema-like file onto the provider's block structure.
        tool_schema {
          dynamic "inline_payload" {
            for_each = local.tools
            content {
              name        = inline_payload.value.name
              description = inline_payload.value.description

              input_schema {
                type = inline_payload.value.inputSchema.type

                dynamic "property" {
                  for_each = try(inline_payload.value.inputSchema.properties, {})
                  content {
                    name        = property.key
                    type        = property.value.type
                    description = try(property.value.description, null)
                    required    = contains(try(inline_payload.value.inputSchema.required, []), property.key)
                  }
                }
              }
            }
          }
        }
      }
    }
  }

  credential_provider_configuration {
    gateway_iam_role {}
  }
}

# Step 5: the `support` target, the FastMCP server on AgentCore Runtime. The Gateway asks it for its
# own tool list. To call it, the Gateway gets a machine token from the credential provider using the
# OAuth "client credentials" flow (the M2M client logs in with its id and secret) and the
# <prefix>/invoke scope.
# The gateway lists the server's tools when the target is created, so the MCP runtime
# must be READY and the credential provider usable by then (implicit deps via ARNs).
resource "aws_bedrockagentcore_gateway_target" "support" {
  gateway_identifier = aws_bedrockagentcore_gateway.this.gateway_id
  name               = "support"
  description        = "Tickets and FAQ (FastMCP on AgentCore Runtime)"

  target_configuration {
    mcp {
      mcp_server {
        endpoint = local.support_endpoint
      }
    }
  }

  credential_provider_configuration {
    oauth {
      provider_arn = var.oauth_provider_arn
      scopes       = var.oauth_scopes
      grant_type   = "CLIENT_CREDENTIALS"
    }
  }

  # Serialize target creation: concurrent changes on one gateway can conflict.
  depends_on = [aws_bedrockagentcore_gateway_target.orders]
}

# ---------------------------------------------------------------- Cedar policies

# Step 6: one policy per infra/policies/*.cedar file (admin_all_tools, m2m_dev_client,
# viewer_read_only). The file name, without .cedar, becomes the policy name. Before upload, the
# {{GATEWAY_ARN}} and {{M2M_CLIENT_ID}} placeholders are replaced with the real Gateway ARN and M2M
# client id. The whole file, comments included, is sent as the policy text.
# The templates are shared with the boto3 path and use {{...}} placeholders, so plain
# replace() on file() instead of templatefile() (which only understands ${...}).
resource "aws_bedrockagentcore_policy" "this" {
  for_each = fileset(var.policies_dir, "*.cedar")

  name             = trimsuffix(each.value, ".cedar") # must match [A-Za-z][A-Za-z0-9_]*
  policy_engine_id = aws_bedrockagentcore_policy_engine.this.policy_engine_id

  definition {
    cedar {
      statement = replace(
        replace(file("${var.policies_dir}/${each.value}"), "{{GATEWAY_ARN}}", aws_bedrockagentcore_gateway.this.gateway_arn),
        "{{M2M_CLIENT_ID}}", var.m2m_client_id
      )
    }
  }

  # CreatePolicy validates action names (orders___get_order, ...) against the
  # gateway's tool schema, so both targets must exist first.
  depends_on = [
    aws_bedrockagentcore_gateway_target.orders,
    aws_bedrockagentcore_gateway_target.support,
  ]
}
