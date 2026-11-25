# 9. Troubleshooting

| Symptom | Likely cause | Section |
|---|---|---|
| `AccessDeniedException` / "You don't have access to the model" | Bedrock model access or wrong region/profile | [Model access](#model-access) |
| `Access denied (HTTP 403)` for carol | Expected | [carol gets 403](#carol-gets-403) |
| 401/403 for alice or bob | Token from the wrong client, expired, or group missing | [401/403 for alice or bob](#401403-for-alice-or-bob) |
| "no Authorization header reached the agent" | Runtime header allowlist missing | [Missing Authorization header](#missing-authorization-header) |
| Policy `CREATE_FAILED` | Action names not on the Gateway | [Policy CREATE_FAILED](#policy-create_failed) |
| "role ... cannot be assumed" / "not authorized to perform" right after creating a role | IAM propagation | [IAM propagation](#iam-propagation) |
| Runtime or Lambda doesn't start / `ImportError` after a deploy | Image or dependency problem | [Images](#images) |
| A GitHub workflow fails (reviewer guard, plan mismatch, Trivy, AccessDenied) | Pipeline | [10-ci-cd.md §8](10-ci-cd.md#8-troubleshooting-the-pipeline) |
| `address already in use` / `[Errno 10048]` | Local port busy | [Port in use](#port-in-use) |

## Model access

The agent calls Bedrock with `MODEL_ID` (default `us.anthropic.claude-haiku-4-5-20251001-v1:0`, a cross-region
inference profile) in `AWS_REGION` (default `us-east-1`).

```bash
aws bedrock list-inference-profiles --region us-east-1 \
  --query "inferenceProfileSummaries[?contains(inferenceProfileId, 'haiku')].[inferenceProfileId,status]" --output table
```

- The profile isn't listed: use a region where it's available, or set `model_id` (in `envs/<env>.tfvars`) /
  `MODEL_ID` (local) to one that is.
- It's listed but invocation fails with `AccessDeniedException`: make sure the account has access to
  the Anthropic model in the Bedrock console (model access, including any first-use form for
  Anthropic models), and that your IAM principal may call `bedrock:InvokeModel`. Some newer models also
  need an agreement accepted per account (that's why the deploy account uses Haiku 4.5, decision 2026-11-07).
- Deployed only: the agent role allows `arn:aws:bedrock:*::foundation-model/*` and inference
  profiles, because a `us.*` profile routes to several regions. An SCP that denies some of those regions
  can still block calls.
- Locally, `scripts/run_local.py` uses your default AWS credentials. Check them with
  `aws sts get-caller-identity`.

## carol gets 403

That's the demo. carol has no Cognito group, and the agent runtime's JWT authorizer requires
`cognito:groups` to contain `Group-ShopAssist-Admin` or `Group-ShopAssist-Viewer`. `scripts/invoke_agent.py --user carol`
prints `Access denied (HTTP 403)` and exits with code 3. `scripts/demo_access.py` counts a 401 or 403
for carol as a pass.

## 401/403 for alice or bob

- **Wrong client.** The agent runtime only accepts tokens from `shopassist-<env>-users`
  (`cognito_users_client_id` in `.deploy/<env>/outputs.json`). An M2M token is rejected there, though the
  Gateway accepts it.
- **Stale `.deploy/`.** After destroying and redeploying, the pool and clients are new. Recreate
  `.deploy/<env>/outputs.json` and `users.json` from the current state ([04 §4](04-deploy-terraform.md#4-getting-deployenv-on-your-machine)).
- **Group membership.** Check with
  `aws cognito-idp admin-list-groups-for-user --user-pool-id <cognito_user_pool_id> --username bob`.
- **"needs a challenge (NEW_PASSWORD_REQUIRED)".** The user has a temporary password. Terraform sets
  permanent passwords. If you created users by hand, run
  `aws cognito-idp admin-set-user-password --permanent ...`.
- **SigV4 against a JWT runtime.** A runtime with a JWT authorizer doesn't accept SigV4, so don't
  use `--sigv4` against it.

## Missing Authorization header

Reply: `Error: no Authorization header reached the agent, so it can't call its tools.` (field
`error: missing_authorization`).

The agent runs with `GATEWAY_AUTH_MODE=passthrough` but didn't receive the header. The agent runtime
needs `requestHeaderConfiguration.requestHeaderAllowlist = ["Authorization"]` (Terraform
`request_header_configuration` in `modules/agent_runtime`). Check it with:

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
  `depends_on` prevents this. If you're working by hand, create the targets first.
- **Unsubstituted placeholders.** A literal `{{GATEWAY_ARN}}` or `{{M2M_CLIENT_ID}}` left in the
  statement.
- **Wrong resource type.** Use `resource is AgentCore::Gateway` or `resource == AgentCore::Gateway::"<arn>"`.

Read the reason:

```bash
aws bedrock-agentcore-control list-policies --policy-engine-id <policy_engine_id> \
  --query "policies[].[name,status,statusReasons]"
```

Fix the `.cedar` file, merge, and deploy again. If the resource is tainted,
Terraform replaces it.

Also check the Gateway role. Without `bedrock-agentcore:GetPolicyEngine`, `AuthorizeAction` and
`PartiallyAuthorizeActions`, gateway creation or every tool call fails (see
https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-permissions.html).

## IAM propagation

New roles and inline policies take a few seconds to become usable, and AgentCore validates the role
on `CreateAgentRuntime` / `CreateGateway`.

- Terraform waits 15 s (`time_sleep.iam`) after each role policy, before the dependent resource.
  If you still hit it, run `deploy.yml` again. Terraform continues where it stopped.

## Images

All workloads are linux/arm64 container images built in GitHub Actions ([10-ci-cd.md §3](10-ci-cd.md#3-how-the-images-are-built)).
Problems usually show up in CI already, because every image is started and called before it can be pushed.

- **Container check fails in CI** (`scripts/ci_image_checks.sh` prints the container logs): read the logs in
  the job output. Typical causes: a missing file in the allow-list `Dockerfile.dockerignore`, or a new import
  whose package isn't in `requirements.in`.
- **Lock-file drift** (`pin_requirements.py --check` fails): run `uv run python scripts/pin_requirements.py`
  after `uv lock`, and commit the `requirements.lock.txt` files.
- **Deployed runtime doesn't start** although CI passed: check the runtime log group
  (`/aws/bedrock-agentcore/runtimes/<id>-DEFAULT`) for an import error or a missing env var, and compare the
  image tag Terraform applied with the one you meant to deploy.
- Images are never built locally (decision 2026-11-07). If you must reproduce a build, use the same
  `docker buildx build --platform linux/arm64 -f <Dockerfile> .` from the repo root on an arm64 machine.

## Hosted chat page

- **Page says "not signed in" or every message returns 401 from the Runtime:** the token comes from the web client
  `shopassist-<env>-web`; its id must be in the Runtime's and the Gateway's `allowed_client_ids` (`main.tf`). Also
  check that the page is opened at exactly the `chat_ui_url` address (trailing slash): Cognito accepts that one
  callback/logout URL only (`redirect_mismatch` otherwise). Tokens last 15 minutes, so sign in again after that.
- **Browser console shows a CORS error on the relay call:** the page must be opened from the CloudFront address
  (the relay allows that origin only), not from a file or another host. Check `ALLOWED_ORIGIN` in the relay's
  Lambda settings.
- **Relay answers 429 or "Rate Exceeded":** the reserved concurrency (`chat_relay_reserved_concurrency`, default 5)
  is used up, usually by long answers in parallel. Retry, or raise the value.
- **Apply fails with "unreserved concurrency … below its minimum" for the relay:** a new or low-limit account
  can't reserve concurrency. Set `chat_relay_reserved_concurrency = null` in `envs/<env>.tfvars` (no cap, so the
  spend limit is gone) or ask AWS to raise the account's Lambda concurrency limit.
- **Old page still shown after a deploy:** the deploy job invalidates the cache; if you changed the page by hand,
  run `aws cloudfront create-invalidation --distribution-id <chat_ui_distribution_id> --paths "/*"`.
- **Relay answers 504:** the agent took longer than 280 s. Ask a shorter question or start a new session.

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

## Other

| Symptom | Fix |
|---|---|
| Terraform: "Error acquiring the state lock" | Another plan/apply holds the `.tflock` file (or a cancelled run left it). Wait, or after checking no run is active, `terraform force-unlock <id>` locally with `personal` |
| Terraform: `ResourceAlreadyExistsException` for `/aws/bedrock-agentcore/runtimes/...-DEFAULT` | The service created the log group first. `terraform import module.agent_runtime.aws_cloudwatch_log_group.runtime <name>` (or `module.mcp_runtime...`), then apply |
| Return demo says 1042 isn't eligible any more | A return was already created. Re-seed with `uv run python scripts/seed_data.py --region <region> --prefix shopassist-<env>` (`demo_access.py` re-seeds unless you pass `--no-reseed`). Locally, restart the emulator (memory store) |
| Agent log shows `blocked tool call <name>` (the model got "Tool '...' is blocked: no activated skill declares the MCP server ...") | Usually normal self-correction: the model called a tool before activating a skill. If it persists, the skill's `metadata.mcp-servers` is missing that server |
| The agent doesn't use a newly uploaded skill | Skills load once per session. Use `--new-session` |
| Gateway lists no `support___*` tools | The MCP runtime wasn't `READY` when the target was created, or the target's OAuth config is wrong. Check the target status, and re-sync with `aws bedrock-agentcore-control synchronize-gateway-targets` |
| Cognito domain `shopassist-<env>-<account>` already taken | Domains are global. Change `name_prefix` in `envs/<env>.tfvars` |
| `agent/app.py` run locally exits silently on the first message (Windows) | Set `PYTHONIOENCODING=utf-8`. Strands' error notes contain characters a cp1252 console can't print. See [11-chat-ui.md](11-chat-ui.md#troubleshooting) |
