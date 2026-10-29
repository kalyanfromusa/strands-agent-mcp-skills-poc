# 5. Deploy with boto3

`infra/boto3/` deploys the same stack as Terraform with plain AWS SDK calls in dependency order:

| File | Purpose |
|---|---|
| `deploy.py` | Runs the steps in order, saves state after each one, writes `.deploy/outputs.json` |
| `destroy.py` | Runs the reverse order, finds resources by name (state is only a shortcut), keeps going on failures |
| `common.py` | Names, tags, `state.json`, progress output, waiting, IAM-propagation retries |
| `steps/*.py` | One module per resource: `s3`, `cognito`, `identity`, `policy`, `iam`, `dynamodb`, `lambda_tools`, `mcp_runtime`, `gateway`, `agent_runtime`, `budget`, `outputs` (+ `runtime_common`) |

Each step module keeps the request dicts in pure `build_*_request()` functions, so you can read the
exact API payloads top to bottom.

> Deploy **one** path at a time. boto3 and Terraform use the same resource names.
> No AWS deployment of this repo has been verified yet. Treat these steps as instructions, and see
> [09-troubleshooting.md](09-troubleshooting.md) if a step fails.

## Prerequisites

| Tool | Check |
|---|---|
| AWS CLI v2 (for the checks below; the deploy itself only needs boto3) | `aws --version`, `aws sts get-caller-identity` |
| uv | `uv --version` |
| Python 3.12 via uv | `uv sync` |

You need credentials allowed to create IAM roles, Lambda, DynamoDB, S3, Cognito, Budgets (optional)
and Bedrock AgentCore resources. Terraform isn't needed.

**Bedrock model access:**

```bash
aws bedrock list-inference-profiles --region us-east-1 \
  --query "inferenceProfileSummaries[?contains(inferenceProfileId, 'haiku')].[inferenceProfileId,status]" --output table
```

`us.anthropic.claude-haiku-5-5` should be listed. If not, pass another profile with `--model-id`.

## 1. Build the artifacts

```bash
uv sync
uv run pytest                         # optional, offline
uv run python scripts/build.py        # dist/agent.zip, dist/support_mcp.zip, dist/orders_lambda.zip
```

`deploy.py` refuses to start if a zip is missing.

## 2. Deploy

```bash
uv run python infra/boto3/deploy.py
```

| Flag | Default | Meaning |
|---|---|---|
| `--region` | `us-east-1` | Region |
| `--prefix` | `shopassist` | Name prefix and `project` tag value (2–21 lowercase letters/digits) |
| `--model-id` | `us.anthropic.claude-haiku-5-5` | Bedrock model or inference profile |
| `--skip-seed` | off | Don't (re)write the demo orders |
| `--budget-email` | none | Create the monthly budget `<prefix>-monthly` (5 USD, alerts at 80% actual and 100% forecasted) |

The script prints `[n/total] step ...`, status changes while it waits, and the time per step:

| # | Step | Why here |
|---|---|---|
| 1 | S3 bucket (versioned, public access blocked), code zips, skills mirror | Everything below reads artifacts from S3 |
| 2 | Cognito pool, domain, resource server, `-m2m` and `-users` clients, groups, alice/bob/carol | OAuth2 server for every JWT. Writes `.deploy/users.json` (passwords reused on re-runs) |
| 3 | AgentCore Identity credential provider `shopassist-cognito` | Holds the M2M secret; needs the domain (discovery `token_endpoint`) |
| 4 | Policy engine `shopassist_policy_engine` | The Gateway references it, and the Gateway role is scoped to it |
| 5 | IAM roles `shopassist-{orders-tools,support-mcp-runtime,gateway,agent-runtime}-role` (same names as Terraform) | Need the provider secret ARN and the engine ARN |
| 6 | DynamoDB tables + `scripts/seed_data.py` | |
| 7 | Lambda `shopassist-orders-tools` + log group | |
| 8 | Runtime `shopassist_support_mcp` + log group | Must be `READY` before the Gateway lists its tools |
| 9 | Gateway `shopassist-gateway`, target `orders`, then target `support` | Targets one at a time |
| 10 | Cedar policies from `infra/policies/*.cedar` | Validated against the Gateway's tools, so after the targets |
| 11 | Runtime `shopassist_agent` + log group | Needs the Gateway URL |
| 12 | Budget (only with `--budget-email`) | |
| 13 | `.deploy/outputs.json` (`deployed_by: "boto3"`) | |

IDs and ARNs are saved to `infra/boto3/state.json` (git-ignored) after every step. If a step fails,
fix the cause and run the same command again. Every step is get-or-create-or-update, so it resumes.
The script refuses to run against a state file that tracks a different prefix or region.

Tags: every resource is created with `project=<prefix>`, passed explicitly in each create call.

## 3. Smoke test

The same commands as for Terraform, from the repo root:

```bash
uv run python scripts/invoke_agent.py --user alice --prompt "Where is order 1042?"
uv run python scripts/invoke_agent.py --user bob   --prompt "I'm C001. Please return the headphones from order 1042."
uv run python scripts/invoke_agent.py --user carol --prompt "Hello"       # expect HTTP 403, exit code 3
uv run python scripts/demo_access.py                                     # access matrix a-d
```

See [04-deploy-terraform.md § Smoke test](04-deploy-terraform.md#4-smoke-test) for what to expect
and the `demo_access.py` flags (`--only`, `--no-reseed`, `--timeout`, `--outputs`, `--users`).

## 4. Update

Re-run `deploy.py` after any change. Each step compares the desired state with what exists and only
calls an update API when something differs:

| Change | Do | What the script does |
|---|---|---|
| Edit, add or remove a skill | `deploy.py` | Uploads changed files (MD5 vs ETag), deletes removed ones. Then start a **new session** (`invoke_agent.py --new-session`) |
| Agent code | `build.py --only agent`, `deploy.py` | New S3 version → `UpdateAgentRuntime`, waits for the new version to be `READY` |
| Orders tools | `build.py --only orders_lambda`, `deploy.py` | `UpdateFunctionCode` when the zip's SHA-256 differs |
| `tools_schema.json` | `deploy.py` | `UpdateGatewayTarget` on `orders` (fingerprint differs) |
| Support MCP server | `build.py --only support_mcp`, `deploy.py` | `UpdateAgentRuntime`, then `SynchronizeGatewayTargets` on `support`, so the Gateway re-lists its tools |
| Cedar policy | `deploy.py` | `UpdatePolicy` if the statement changed, `CreatePolicy` for a new file, `DeletePolicy` for a removed file |
| Model | `deploy.py --model-id <profile>` | Agent runtime env var update |
| Re-seed only | `uv run python scripts/seed_data.py --region <region>` | |

Use `--skip-seed` on re-runs if you don't want the demo orders reset.

## 5. Destroy

```bash
uv run python infra/boto3/destroy.py            # asks you to type the prefix
uv run python infra/boto3/destroy.py --yes      # no prompt
```

`--region` and `--prefix` default to the values in `state.json`, falling back to `us-east-1` /
`shopassist`.

Order: Cedar policies → Gateway targets → Gateway → policy engine → agent runtime → support MCP
runtime → credential provider (its managed secret goes with it) → Lambda → DynamoDB → Cognito
domain, then pool → leftover log groups → IAM roles → S3 bucket (every object version) → budget →
local files (`.deploy/outputs.json` if boto3 wrote it, `.deploy/users.json`, `state.json`).

Every step treats "not found" as success and a failing step doesn't stop the others. Failures are
listed at the end with exit code 1, and the state file is kept, so re-run until it reports success.
Because resources are looked up by name, destroy also works if `state.json` is lost.

## 6. Verify nothing is left

```bash
aws resourcegroupstaggingapi get-resources --region us-east-1 \
  --tag-filters Key=project,Values=shopassist \
  --query "ResourceTagMappingList[].ResourceARN"
```

Expect `[]`. The index can lag a few minutes, and IAM roles and Cognito domains don't always show up
there. The name-based checks in [04-deploy-terraform.md § Verify](04-deploy-terraform.md#7-verify-nothing-is-left)
apply unchanged. Resource and role names are the same in both paths.
Budgets don't appear in the tagging API. Check with
`aws budgets describe-budgets --account-id <account> --query "Budgets[].BudgetName"`.
