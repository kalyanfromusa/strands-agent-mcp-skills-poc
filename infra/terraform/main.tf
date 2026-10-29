# =====================================================================================================
# main.tf (Terraform root module): the "table of contents" of the ShopAssist deployment
#
# What this file does
#   Wires the six building blocks (modules) of ShopAssist together, adds an optional cost alert, and
#   writes two small files into .deploy/ that the command-line scripts read to talk to the deployment.
#
# Where it fits
#   You run `terraform apply` in infra/terraform after `uv run python scripts/build.py` has built the
#   zips in dist/. Terraform reads every *.tf file in this folder (plus the modules it points to),
#   creates the AWS resources, then writes .deploy/outputs.json and .deploy/users.json. After that,
#   scripts/invoke_agent.py and scripts/demo_access.py use those two files. The boto3 scripts in
#   infra/boto3 build the same thing step by step, in Python, and write the same files.
#
# How Terraform works (the declarative story)
#   Terraform code describes the end result, not the steps. Terraform reads all the files, notices
#   which resource uses a value from which other resource (for example `module.data.orders_table_arn`),
#   and builds a dependency graph from that. It then creates resources in graph order, running
#   independent ones in parallel. `depends_on` adds an ordering rule where no value is passed. The
#   state file (terraform.tfstate, git-ignored) records what was created, so the next `apply` only
#   changes what differs, and `terraform destroy` removes everything in reverse order.
#
# How the modules feed each other (an arrow means "outputs of A are inputs of B")
#
#   data (S3 bucket, zips, skills, DynamoDB tables)
#     |-- orders zip + orders/returns tables --> lambda_tools (orders Lambda) --+
#     |-- support zip + tickets table -------> mcp_runtime (support server) --+ |
#     |                                                                        | | Lambda ARN,
#     |                                                                        v v runtime ARN
#   identity (Cognito, demo users, OAuth provider) -- login settings -------> gateway
#     |                                                  (MCP endpoint + Cedar policies)
#     |                                                                        |
#     |                                                                        | gateway URL
#     |                                                                        v
#     +-- login settings, users client id --------------------------------> agent_runtime
#   data -- agent zip + skills location -------------------------------------> (Strands agent)
#
#   All of the above --> .deploy/outputs.json and .deploy/users.json (files on your computer)
#
# Step by step (the order Terraform ends up following)
#   1. Look up the AWS account id of whoever is running Terraform.
#   2. module.data: create the S3 bucket, upload the zips and skills, create the 3 DynamoDB tables,
#      and (optionally) fill the orders table with demo data.
#   3. module.identity: create the Cognito user pool, app clients, demo users alice/bob/carol and
#      the AgentCore Identity OAuth2 credential provider. (Runs in parallel with step 2.)
#   4. module.lambda_tools: create the `orders` tools Lambda function from the uploaded zip.
#   5. module.mcp_runtime: start the `support` tools server on AgentCore Runtime.
#   6. module.gateway: create the policy engine, the Gateway, its two targets and the Cedar policies.
#   7. module.agent_runtime: start the Strands agent on AgentCore Runtime, pointed at the Gateway.
#   8. Optionally create a monthly AWS Budget alert.
#   9. Write .deploy/outputs.json and .deploy/users.json for the CLI scripts.
#
# Key terms
#   Module: a folder of Terraform files that builds one part of the system; called like a function.
#   Resource: one thing in AWS (a bucket, a table, a role) that Terraform creates and tracks.
#   Data source: a read-only lookup of something that already exists (here: the account id).
#   locals: named values computed once and reused, like named cells in a spreadsheet.
#   ARN (Amazon Resource Name): the unique id of an AWS resource, e.g. arn:aws:lambda:...:function:x.
#   MCP (Model Context Protocol): a standard way for an AI agent to discover and call tools.
#   Gateway: AgentCore Gateway, one MCP address that fronts all of ShopAssist's tools.
#   Runtime: AgentCore Runtime, the AWS service that runs our Python code (the agent or a tool server).
#   Cognito: the AWS login service; it issues the tokens that prove who a user is.
#   JWT (JSON Web Token): a signed login token that says who the user is and which groups they are in.
# =====================================================================================================

# Step 1: ask AWS "which account am I deployed into?". The account id is used in globally unique names
# (for example the S3 bucket name) and in permission rules.
data "aws_caller_identity" "current" {}

# Values computed once and reused below.
#   repo_root:   the repository folder, two levels up from infra/terraform.
#   artifacts:   the zip files built by scripts/build.py. Terraform does not build code; it only uploads
#                these files, and stops with a clear message if they are missing (see modules/data).
#   oauth_scope: the permission name ("shopassist/invoke") written into machine tokens. The Gateway asks
#                for it when it calls the support server.
locals {
  account_id = data.aws_caller_identity.current.account_id
  repo_root  = abspath("${path.root}/../..")

  # Produced by scripts/build.py. Terraform only consumes them (see modules/data).
  artifacts = {
    agent         = "${local.repo_root}/dist/agent.zip"
    support_mcp   = "${local.repo_root}/dist/support_mcp.zip"
    orders_lambda = "${local.repo_root}/dist/orders_lambda.zip"
  }

  oauth_scope = "${var.name_prefix}/invoke"
}

# Step 2: storage. Creates the artifacts S3 bucket (code zips + skills files) and the three DynamoDB
# tables (orders, returns, tickets), then optionally runs scripts/seed_data.py to load demo orders.
# The other modules read zip locations and table names from this module's outputs.
# Storage: artifacts bucket (zips + skills) and the three DynamoDB tables.
module "data" {
  source = "./modules/data"

  name_prefix    = var.name_prefix
  region         = var.region
  account_id     = local.account_id
  repo_root      = local.repo_root
  artifact_files = local.artifacts
  skills_dir     = "${local.repo_root}/skills"
  seed_file      = "${local.repo_root}/data/seed.json"
  seed_data      = var.seed_data
}

# Step 3: logins. Cognito issues login tokens for the demo users (alice, bob, carol) and for the
# machine-to-machine (M2M) client. The AgentCore Identity credential provider stores the M2M secret
# so the Gateway can get its own token when it calls the support server. This module needs nothing
# from the other modules, so Terraform builds it in parallel with module "data".
# Cognito (the OAuth2 authorization server) + AgentCore Identity credential provider.
module "identity" {
  source = "./modules/identity"

  name_prefix = var.name_prefix
  region      = var.region
  account_id  = local.account_id
}

# Step 4: the `orders` tools (get_order, list_customer_orders, check_return_eligibility,
# create_return) as an AWS Lambda function. It needs the uploaded zip and the orders/returns table
# names from module "data", so Terraform waits for that module first.
# `orders` tools as a Lambda behind the Gateway.
module "lambda_tools" {
  source = "./modules/lambda_tools"

  name_prefix        = var.name_prefix
  bucket             = module.data.artifacts_bucket
  zip_key            = module.data.artifact_keys["orders_lambda"]
  zip_version_id     = module.data.artifact_version_ids["orders_lambda"]
  orders_table_arn   = module.data.orders_table_arn
  orders_table_name  = module.data.orders_table_name
  returns_table_arn  = module.data.returns_table_arn
  returns_table_name = module.data.returns_table_name
  log_retention_days = var.log_retention_days
}

# Step 5: the `support` tools (create_ticket, get_ticket, search_faq) as a FastMCP server running on
# AgentCore Runtime. It needs the zip and the tickets table (module "data") and the Cognito details
# (module "identity"), because it only accepts calls that carry a token from the M2M client.
# `support` tools as a FastMCP server on AgentCore Runtime (protocol MCP).
module "mcp_runtime" {
  source = "./modules/mcp_runtime"

  name_prefix        = var.name_prefix
  region             = var.region
  account_id         = local.account_id
  bucket             = module.data.artifacts_bucket
  zip_key            = module.data.artifact_keys["support_mcp"]
  zip_version_id     = module.data.artifact_version_ids["support_mcp"]
  tickets_table_arn  = module.data.tickets_table_arn
  tickets_table_name = module.data.tickets_table_name
  discovery_url      = module.identity.discovery_url
  allowed_client_ids = [module.identity.client_id]
  log_retention_days = var.log_retention_days
}

# Step 6: the Gateway, the one MCP address the agent talks to. It brings together the Lambda from
# step 4, the support server from step 5 and the Cognito and credential-provider details from step 3.
# It also loads the Cedar policy files from infra/policies, which decide which user may see and call
# which tool. Because it uses outputs from three modules, Terraform creates it after all of them.
# Single MCP endpoint for the agent: targets `orders` (Lambda) and `support` (MCP
# runtime), guarded by a JWT authorizer and a Cedar policy engine (design.md section 8).
module "gateway" {
  source = "./modules/gateway"

  name_prefix = var.name_prefix
  region      = var.region
  account_id  = local.account_id

  discovery_url = module.identity.discovery_url
  # users client: tokens forwarded by the agent; m2m client: local dev and smoke tests.
  allowed_client_ids = [module.identity.users_client_id, module.identity.client_id]

  lambda_arn        = module.lambda_tools.function_arn
  tools_schema_path = "${local.repo_root}/mcp_servers/orders_lambda/tools_schema.json"

  mcp_runtime_arn       = module.mcp_runtime.runtime_arn
  oauth_provider_arn    = module.identity.oauth_provider_arn
  oauth_provider_secret = module.identity.oauth_provider_secret_arn
  oauth_scopes          = [local.oauth_scope]

  policies_dir  = "${local.repo_root}/infra/policies"
  m2m_client_id = module.identity.client_id
}

# Step 7: the ShopAssist agent itself, running on AgentCore Runtime. It is created last because it
# needs the Gateway URL (step 6) to find its tools, and the skills location (step 2) to load its
# procedures. Only users in the shop-admin or shop-viewer Cognito group may call it, so carol (no
# group) is turned away before any agent code runs.
# The generic Strands agent (protocol HTTP), invoked with a Cognito user token.
module "agent_runtime" {
  source = "./modules/agent_runtime"

  name_prefix    = var.name_prefix
  region         = var.region
  account_id     = local.account_id
  bucket         = module.data.artifacts_bucket
  zip_key        = module.data.artifact_keys["agent"]
  zip_version_id = module.data.artifact_version_ids["agent"]
  skills_prefix  = module.data.skills_prefix
  skills_s3_uri  = module.data.skills_s3_uri
  model_id       = var.model_id
  gateway_url    = module.gateway.gateway_url

  discovery_url      = module.identity.discovery_url
  allowed_client_ids = [module.identity.users_client_id]
  allowed_groups     = ["shop-admin", "shop-viewer"]

  oauth_provider_name = module.identity.oauth_provider_name
  oauth_scopes        = [local.oauth_scope]
  log_retention_days  = var.log_retention_days
}

# Step 8 (optional): a monthly AWS Budget that emails you when spending passes 80% of the limit, or
# when AWS forecasts that it will pass 100%. `count = 0` means "do not create it", so the budget only
# exists when enable_budget = true. A budget only sends alerts; it never stops or deletes anything.
# Optional guard-rail. Account-wide (not tag-filtered) because cost allocation
# tags must be activated manually in Billing before a tag filter sees anything.
resource "aws_budgets_budget" "monthly" {
  count = var.enable_budget ? 1 : 0

  name         = "${var.name_prefix}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Alert 1: money actually spent this month is above 80% of the limit.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
  }

  # Alert 2: AWS predicts that the month will end above 100% of the limit.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }
}

# Step 9a: collect the addresses and ids that the CLI scripts need into one map. The key names are a
# fixed contract, so the scripts work the same whether Terraform or boto3 did the deployment.
locals {
  # Contract: design.md section 6. scripts/invoke_agent.py reads this file,
  # and the boto3 path writes the same keys with deployed_by = "boto3".
  deploy_outputs = {
    region                  = var.region
    account_id              = local.account_id
    artifacts_bucket        = module.data.artifacts_bucket
    skills_s3_uri           = module.data.skills_s3_uri
    agent_runtime_arn       = module.agent_runtime.runtime_arn
    support_mcp_runtime_arn = module.mcp_runtime.runtime_arn
    gateway_id              = module.gateway.gateway_id
    gateway_url             = module.gateway.gateway_url
    cognito_user_pool_id    = module.identity.user_pool_id
    cognito_client_id       = module.identity.client_id
    cognito_token_url       = module.identity.token_url
    oauth_provider_name     = module.identity.oauth_provider_name
    cognito_users_client_id = module.identity.users_client_id
    policy_engine_id        = module.gateway.policy_engine_id
    deployed_by             = "terraform"
  }
}

# Step 9b: write that map to .deploy/outputs.json on your own computer (not in AWS). `local_file` is a
# Terraform resource for a file on disk, so `terraform destroy` deletes it again. It holds no secrets.
resource "local_file" "deploy_outputs" {
  filename        = "${path.root}/../../.deploy/outputs.json"
  content         = jsonencode(local.deploy_outputs)
  file_permission = "0644"
}

# Step 9c: write the demo users' names, passwords and groups to .deploy/users.json, so the scripts can
# log in as alice, bob or carol. "Sensitive" hides the content from Terraform's screen output, and
# permission 0600 means only your own user account may read the file (on Linux and macOS).
# Demo credentials (design.md section 8). .deploy/ is git-ignored; the passwords
# also live in Terraform state, which is acceptable for throw-away demo users.
resource "local_sensitive_file" "demo_users" {
  filename        = "${path.root}/../../.deploy/users.json"
  content         = jsonencode(module.identity.demo_users)
  file_permission = "0600"
}
