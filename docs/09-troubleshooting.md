# 9. Troubleshooting

| Symptom | Likely cause | Section |
|---|---|---|
| `AccessDeniedException` / "You don't have access to the model" | Bedrock model access or wrong region/profile | [Model access](#model-access) |
| `Access denied (HTTP 403)` for carol | Expected | [carol gets 403](#carol-gets-403) |
| 401/403 for alice or bob | Token from the wrong client, expired, or group missing | [401/403 for alice or bob](#401403-for-alice-or-bob) |
| "no Authorization header reached the agent" | Runtime header allowlist missing | [Missing Authorization header](#missing-authorization-header) |
| Policy `CREATE_FAILED` | Action names not on the Gateway | [Policy CREATE_FAILED](#policy-create_failed) |
| "role ... cannot be assumed" / "not authorized to perform" right after creating a role | IAM propagation | [IAM propagation](#iam-propagation) |
| Runtime starts but crashes with `ImportError` / "invalid ELF header" | Wrong-platform wheels | [arm64 wheels](#arm64-wheels) |
| `address already in use` / `[Errno 10048]` | Local port busy | [Port in use](#port-in-use) |
| Resources disappear, credentials stop working, can't create a service | Workshop Studio account limits or expiry | [Workshop Studio accounts](#workshop-studio-accounts) |

## Model access

The agent calls Bedrock with `MODEL_ID` (default `us.anthropic.claude-haiku-5-5`, a cross-region
inference profile) in `AWS_REGION` (default `us-east-1`).

```bash
aws bedrock list-inference-profiles --region us-east-1 \
  --query "inferenceProfileSummaries[?contains(inferenceProfileId, 'haiku')].[inferenceProfileId,status]" --output table
```

- The profile isn't listed: use a region where it's available, or set `model_id` (Terraform) /
  `--model-id` (boto3) / `MODEL_ID` (local) to one that is.
- It's listed but invocation fails with `AccessDeniedException`: make sure the account has access to
  the Anthropic model in the Bedrock console (model access, including any first-use form for
  Anthropic models), and that your IAM principal may call `bedrock:InvokeModel`.
- Deployed only: the agent role allows `arn:aws:bedrock:*::foundation-model/*` and inference
  profiles, because a `us.*` profile routes to several regions. An SCP that denies some of those regions
  can still block calls.
- Locally, `scripts/run_local.py` uses your default AWS credentials. Check them with
  `aws sts get-caller-identity`.

## carol gets 403

That's the demo. carol has no Cognito group, and the agent runtime's JWT authorizer requires
`cognito:groups` to contain `shop-admin` or `shop-viewer`. `scripts/invoke_agent.py --user carol`
prints `Access denied (HTTP 403)` and exits with code 3. `scripts/demo_access.py` counts a 401 or 403
for carol as a pass.

## 401/403 for alice or bob

- **Wrong client.** The agent runtime only accepts tokens from `shopassist-users`
  (`cognito_users_client_id` in `.deploy/outputs.json`). An M2M token is rejected there, though the
  Gateway accepts it.
- **Stale `.deploy/`.** After destroying and redeploying, the pool and clients are new. Make sure
  `.deploy/outputs.json` and `.deploy/users.json` come from the current deployment.
- **Group membership.** Check with
  `aws cognito-idp admin-list-groups-for-user --user-pool-id <cognito_user_pool_id> --username bob`.
- **"needs a challenge (NEW_PASSWORD_REQUIRED)".** The user has a temporary password. Both
  deploy paths set permanent passwords. If you created users by hand, run
  `aws cognito-idp admin-set-user-password --permanent ...`.
- **SigV4 against a JWT runtime.** A runtime with a JWT authorizer doesn't accept SigV4, so don't
  use `--sigv4` against it.

## Missing Authorization header

Reply: `Error: no Authorization header reached the agent, so it can't call its tools.` (field
`error: missing_authorization`).

The agent runs with `GATEWAY_AUTH_MODE=passthrough` but didn't receive the header. The agent runtime
needs `requestHeaderConfiguration.requestHeaderAllowlist = ["Authorization"]` (Terraform
`request_header_configuration`, boto3 `steps/agent_runtime.py`). Check it with:

```bash
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id <id> --query requestHeaderConfiguration
```

Locally, `run_local.py` defaults to `GATEWAY_AUTH_MODE=none`, so this doesn't apply there. If you set
`passthrough` locally, there's no header to pass through. Use `none` with the emulator or
`client_credentials` with the real Gateway.

## Policy CREATE_FAILED

`CreatePolicy` validates the Cedar statement against the policy engine's schema, which includes
the tools of the Gateway the engine is attached to. Common causes:

- **Unknown action names.** `AgentCore::Action::"orders___get_ordr"` (a typo), a tool that isn't in
  `tools_schema.json`, or a `support___*` tool the Gateway hasn't listed yet. Action names are the
  full Gateway names with three underscores.
- **Targets not ready.** The policies were created before both targets were `READY`. Terraform
  `depends_on` and the boto3 step order prevent this. If you're working by hand, create the targets first.
- **Unsubstituted placeholders.** A literal `{{GATEWAY_ARN}}` or `{{M2M_CLIENT_ID}}` left in the
  statement.
- **Wrong resource type.** Use `resource is AgentCore::Gateway` or `resource == AgentCore::Gateway::"<arn>"`.

Read the reason:

```bash
aws bedrock-agentcore-control list-policies --policy-engine-id <policy_engine_id> \
  --query "policies[].[name,status,statusReasons]"
```

The boto3 path prints `statusReasons` and recreates a policy that is `CREATE_FAILED` on the next
run. With Terraform, fix the `.cedar` file and `terraform apply` again. If the resource is tainted,
Terraform replaces it.

Also check the Gateway role. Without `bedrock-agentcore:GetPolicyEngine`, `AuthorizeAction` and
`PartiallyAuthorizeActions`, gateway creation or every tool call fails (see
https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-permissions.html).

## IAM propagation

New roles and inline policies take a few seconds to become usable, and AgentCore validates the role
on `CreateAgentRuntime` / `CreateGateway`.

- Terraform waits 15 s (`time_sleep.iam`) after each role policy, before the dependent resource.
  If you still hit it, run `terraform apply` again. It continues where it stopped.
- boto3 retries IAM-looking errors (`AccessDenied`, `ValidationException` mentioning the role, ...)
  with backoff for up to 2 minutes (`call_with_iam_retry` in `infra/boto3/common.py`). Re-running the
  deploy is safe.

## arm64 wheels

AgentCore Runtime runs the zips on **Linux arm64, Python 3.12**. `scripts/build.py` installs
dependencies with `--python-platform aarch64-manylinux2014 --python-version 3.12 --only-binary=:all:`.

- `No solution found` / "no wheels with a matching platform tag": a dependency (or one of its
  dependencies) has no `manylinux aarch64` cp312 wheel. Pin a version that ships one, or replace it.
- Runtime log shows `invalid ELF header`, `cannot open shared object file` or a pydantic-core import
  error: the zip has wheels for the wrong platform, usually from a manual `pip install` into the
  folder. Rebuild with `uv run python scripts/build.py`.
- Paths in the zip must use forward slashes. `build.py` writes them that way on Windows too. Don't
  re-zip `build/` with other tools.
- `uv not found on PATH`: install uv (https://docs.astral.sh/uv/).

## Port in use

`scripts/local_gateway.py` listens on `localhost:8000` by default, which often clashes with other
local services:

```bash
uv run python scripts/local_gateway.py --port 8765
uv run python scripts/run_local.py --gateway-url http://localhost:8765/mcp
```

`--role viewer` on a second port lets you compare admin and viewer behaviour side by side. If
`run_local.py` reports a connection error or MCP startup timeout, check that the emulator is running
and that the URL ends in `/mcp`.

## Workshop Studio accounts

Temporary event accounts (AWS Workshop Studio and similar) behave differently from your own account:

- **Expiry.** Credentials last a few hours and the account is wiped at the end of the event. Destroy
  before it ends if you care about a clean teardown. The account is removed either way.
- **Expired credentials mid-deploy.** Refresh them, then re-run `terraform apply` or the boto3
  deploy. Both continue from where they stopped (Terraform state / `infra/boto3/state.json`).
- **Service and region restrictions.** SCPs may block some services, regions or actions (for example
  Budgets, Cognito domains, AgentCore Policy, or Bedrock models outside the event's region). If a call
  fails with an explicit deny from an SCP, you can't fix it inside the account. Use the allowed region,
  or skip the optional piece (`enable_budget = false`).
- **Model access** may be preconfigured for specific models only. Check with
  `aws bedrock list-inference-profiles` first.
- **Quotas** (for example AgentCore runtimes per account) may be lower than in a normal account.

## Other

| Symptom | Fix |
|---|---|
| Terraform: `Missing .../dist/agent.zip` | Run `uv run python scripts/build.py` first. Terraform doesn't build artifacts |
| Terraform: `ResourceAlreadyExistsException` for `/aws/bedrock-agentcore/runtimes/...-DEFAULT` | The service created the log group first. `terraform import module.agent_runtime.aws_cloudwatch_log_group.runtime <name>` (or `module.mcp_runtime...`), then apply |
| Return demo says 1042 isn't eligible any more | A return was already created. Re-seed with `uv run python scripts/seed_data.py --region <region>` (`demo_access.py` re-seeds unless you pass `--no-reseed`). Locally, restart the emulator (memory store) |
| Agent log shows `blocked tool call <name>` (the model got "Tool '...' is blocked: no activated skill declares the MCP server ...") | Usually normal self-correction: the model called a tool before activating a skill. If it persists, the skill's `metadata.mcp-servers` is missing that server |
| The agent doesn't use a newly uploaded skill | Skills load once per session. Use `--new-session` |
| Gateway lists no `support___*` tools | The MCP runtime wasn't `READY` when the target was created, or the target's OAuth config is wrong. Check the target status, and re-sync with `aws bedrock-agentcore-control synchronize-gateway-targets` |
| Cognito domain `shopassist-<account>` already taken | Domains are global. Change the prefix (Terraform `name_prefix`, boto3 `--prefix`) |
