# 4. Deploy with Terraform

The Terraform stack is in `infra/terraform/` (root module plus `modules/{data,identity,lambda_tools,mcp_runtime,gateway,agent_runtime}`).
It writes `.deploy/outputs.json` and `.deploy/users.json`, the same files the boto3 path writes, so
the client scripts work with either.

> Deploy **one** path at a time. Terraform and boto3 use the same resource names.
> No AWS deployment of this repo has been verified yet. Treat these steps as instructions, and see
> [09-troubleshooting.md](09-troubleshooting.md) if a step fails.

## Prerequisites

| Tool | Version | Check |
|---|---|---|
| AWS CLI | v2 | `aws --version`, `aws sts get-caller-identity` |
| uv | recent | `uv --version` (Terraform's seed step and the build both call it) |
| Terraform | ≥ 1.9 (`hashicorp/aws` ≥ 6.71, < 7.0) | `terraform version` |
| Python | 3.12 (uv installs it if needed) | |

You need AWS credentials allowed to create IAM roles, Lambda, DynamoDB, S3, Cognito, Secrets Manager
(through AgentCore Identity), CloudWatch Logs and Bedrock AgentCore resources (an admin role in a
sandbox account is simplest).

**Bedrock model access.** Check that the inference profile exists in your region and that you can
use it:

```bash
aws bedrock list-inference-profiles --region us-east-1 \
  --query "inferenceProfileSummaries[?contains(inferenceProfileId, 'haiku')].[inferenceProfileId,status]" --output table
```

`us.anthropic.claude-haiku-5-5` should be listed. If not, choose another profile and set `model_id`
(below). It's worth testing the model locally first ([README quick start](../README.md#quick-start)),
because that exercises the same Bedrock access.

## 1. Build the artifacts

From the repo root:

```bash
uv sync                               # dev environment (tests, local run)
uv run pytest                         # optional, offline
uv run python scripts/build.py        # dist/agent.zip, dist/support_mcp.zip, dist/orders_lambda.zip
```

Terraform doesn't build anything. If a zip is missing, `terraform plan` fails with
`Missing .../dist/<name>.zip ... run uv run python scripts/build.py`.

## 2. Configure (optional)

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars    # git-ignored
```

| Variable | Default | Meaning |
|---|---|---|
| `region` | `us-east-1` | Region for every resource |
| `name_prefix` | `shopassist` | Resource-name prefix and `project` tag value. 2–21 lowercase letters/digits |
| `model_id` | `us.anthropic.claude-haiku-5-5` | Bedrock model or inference profile for the agent |
| `log_retention_days` | `7` | CloudWatch retention for Lambda and runtime log groups |
| `seed_data` | `true` | Run `scripts/seed_data.py` (local-exec, needs `uv` on PATH) after the tables exist |
| `enable_budget` | `false` | Create the account-wide monthly budget `<prefix>-monthly` |
| `budget_email` | `""` | Alert address (required when `enable_budget = true`) |
| `budget_limit_usd` | `5` | Budget limit |

## 3. Deploy

```bash
cd infra/terraform
terraform init
terraform plan -out tfplan
terraform apply tfplan
```

What gets created, roughly in dependency order (Terraform works the order out from references,
plus a few explicit `depends_on`):

1. `module.data`: versioned S3 bucket `shopassist-artifacts-<account>-<region>`, the three zips under
   `artifacts/`, every file in `skills/` under `skills/`, three DynamoDB tables, and the seed run.
2. `module.identity`: Cognito pool, domain `shopassist-<account>`, resource server and scope,
   `shopassist-m2m` and `shopassist-users` clients, groups, alice/bob/carol with random permanent
   passwords, and the AgentCore Identity OAuth2 credential provider `shopassist-cognito`.
3. `module.lambda_tools`: role, 7-day log group, Lambda `shopassist-orders-tools`.
4. `module.mcp_runtime`: role, 15 s IAM wait, Runtime `shopassist_support_mcp` (MCP, JWT
   authorizer for the M2M client), log group.
5. `module.gateway`: policy engine `shopassist_policy_engine`, Gateway role, 15 s IAM wait, Gateway
   `shopassist-gateway` (CUSTOM_JWT, policy engine in `ENFORCE`), targets `orders` then `support`, and
   one `aws_bedrockagentcore_policy` per `infra/policies/*.cedar`.
6. `module.agent_runtime`: role, 15 s IAM wait, Runtime `shopassist_agent` (HTTP, JWT authorizer with
   the `cognito:groups` claim rule, `Authorization` header allowlist, env vars), log group.
7. Optional `aws_budgets_budget.monthly`. Then `local_file.deploy_outputs` writes `.deploy/outputs.json`
   and `local_sensitive_file.demo_users` writes `.deploy/users.json`.

Every taggable resource gets `project=shopassist` through the provider's `default_tags`.

Useful outputs:

```bash
terraform output                    # contract values, file paths
terraform output gateway_url
```

## 4. Smoke test

From the repo root (the scripts read `.deploy/outputs.json` and `.deploy/users.json`):

```bash
uv run python scripts/invoke_agent.py --user alice --prompt "Where is order 1042?"
uv run python scripts/invoke_agent.py --user bob   --prompt "I'm C001. Please return the headphones from order 1042."
uv run python scripts/invoke_agent.py --user carol --prompt "Hello"
```

Expected:

| User | Expected result |
|---|---|
| alice | Delivery status and date of 1042, typically with the dim line `skills: order-tracking \| tools: orders___get_order` |
| bob | Eligibility and refund amount, then an explanation that his account can't create returns; no `orders___create_return` in the tools line |
| carol | `Access denied (HTTP 403): user 'carol' is not allowed to use this agent.`, exit code 3 |

Without `--prompt`, `invoke_agent.py` is interactive (`/new` starts a new session, `exit` quits).
Other flags: `--new-session`, `--arn`, `--region`, `--outputs`, and `--sigv4` (for a runtime
without a JWT authorizer; not this one).

Then run the access matrix:

```bash
uv run python scripts/demo_access.py
```

It re-seeds the orders table (unless you pass `--no-reseed`), then runs, in the order b, c, d, a:

| # | Scenario | Pass condition | Enforced by |
|---|---|---|---|
| a | alice asks to return the 1042 headphones | `orders___create_return` used | all layers allow |
| b | bob asks the same | No `create_return`, and the reply mentions permission | Policy hides the tool, skill explains |
| c | carol calls the agent | HTTP 401/403 | Runtime JWT authorizer (claim rule) |
| d | Direct Gateway calls, no agent | bob's `tools/list` lacks the 2 write tools, bob's `tools/call orders___create_return` is denied, alice's `tools/list` has all 7 | AgentCore Policy |

It prints a table and exits non-zero if any outcome is unexpected. `--only b,d` runs a subset, and
`--timeout` sets the per-call timeout (default 300 s).

The first call of a session includes a cold start (microVM, S3 skill sync, Gateway tool listing),
so it's noticeably slower than the next ones.

## 5. Update

| Change | Do | Effect |
|---|---|---|
| Edit or add a skill | `terraform apply` | Only the changed `aws_s3_object.skill` objects. Start a **new session** (`--new-session`), because skills load once per session |
| Agent code (`agent/`) | `uv run python scripts/build.py --only agent`, then `terraform apply` | New S3 object version → in-place update of `shopassist_agent` (new runtime version; `DEFAULT` follows it) |
| Orders tools (`handler.py`, `common/`) | `build.py --only orders_lambda`, `terraform apply` | Lambda code update |
| `tools_schema.json` | `terraform apply` | `orders` target's inline schema updated |
| Support MCP server | `build.py --only support_mcp`, `terraform apply` | Runtime update. If its **tool list** changed, re-sync the target: `aws bedrock-agentcore-control synchronize-gateway-targets --gateway-identifier <gateway_id> --target-id-list <support target id>` |
| Cedar policy (`infra/policies/*.cedar`) | `terraform apply` | Policy updated in place. A new file becomes a new policy |
| Model | `terraform apply -var model_id=<profile>` (or in `terraform.tfvars`) | Agent runtime env var update |
| Re-seed demo data | `uv run python scripts/seed_data.py --region <region>` | Resets order data and return windows. `seed_data.py` only re-runs from Terraform when `data/seed.json` changes |

Always run `terraform plan` first. A change to a runtime's code shows as an in-place update
(`version_id`), not a replacement.

## 6. Destroy

```bash
cd infra/terraform
terraform destroy
```

The bucket has `force_destroy = true`, so its object versions are removed too. Terraform also deletes
`.deploy/outputs.json` and `.deploy/users.json`. `.deploy/session-*.txt` stays, and is harmless.

## 7. Verify nothing is left

Everything taggable has `project=shopassist`:

```bash
aws resourcegroupstaggingapi get-resources --region us-east-1 \
  --tag-filters Key=project,Values=shopassist \
  --query "ResourceTagMappingList[].ResourceARN"
```

An empty list `[]` is what you want. The tagging index can lag by a few minutes, and some
resources (IAM roles are global; Cognito domains and Lambda log streams aren't tagged) don't always
show up, so also check by name:

```bash
aws bedrock-agentcore-control list-agent-runtimes --query "agentRuntimes[].agentRuntimeName"
aws bedrock-agentcore-control list-gateways --query "items[].name"
aws bedrock-agentcore-control list-policy-engines --query "policyEngines[].name"
aws bedrock-agentcore-control list-oauth2-credential-providers --query "credentialProviders[].name"
aws iam list-roles --query "Roles[?starts_with(RoleName, 'shopassist')].RoleName"
aws logs describe-log-groups --log-group-name-prefix /aws/bedrock-agentcore/runtimes/shopassist --query "logGroups[].logGroupName"
aws logs describe-log-groups --log-group-name-prefix /aws/lambda/shopassist --query "logGroups[].logGroupName"
aws cognito-idp list-user-pools --max-results 60 --query "UserPools[?Name=='shopassist-pool'].Id"
aws s3 ls | grep shopassist
```

If something is left (for example a log group the service recreated after Terraform deleted it),
delete it by hand, or run `uv run python infra/boto3/destroy.py --yes`, which finds resources by
name and treats "already gone" as success.

## Verified run (2026-10-29)

Deployed with Terraform to a Workshop Studio account in us-east-1, then destroyed. `scripts/demo_access.py`:

| # | Check | Enforced by | Result |
|---|---|---|---|
| a | alice: agent creates the return | all layers allow (admin) | ✅ `get_order` → `check_return_eligibility` → `create_return` |
| b | bob: no return, permission explained | AgentCore Policy hides the tool, and the skill guidance explains | ✅ no `create_return` |
| c | carol: agent call rejected | Runtime JWT authorizer (`cognito:groups` claim) | ✅ HTTP 401 |
| d | bob: Gateway `tools/list` | AgentCore Policy filters the list per user | ✅ 5 tools, write tools hidden |
| d | bob: Gateway `tools/call orders___create_return` | AgentCore Policy (Cedar) | ✅ "Tool call not allowed due to policy enforcement (denied by default)" |
| d | alice: Gateway `tools/list` | all layers allow | ✅ 7 tools |

Two first-deploy issues were fixed in Terraform: an empty `protocol_configuration { mcp {} }` is rejected by the
API, so it is omitted; and AgentCore creates the runtime log groups itself, so `scripts/log_group.py` sets their
retention and deletes them on destroy.
