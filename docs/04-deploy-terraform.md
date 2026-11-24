# 4. Deploy with Terraform

Terraform is the **only** way ShopAssist is deployed. In normal use you don't run it yourself:
the GitHub Actions workflow `deploy.yml` runs it for you, with an approval step
([10-ci-cd.md](10-ci-cd.md)). This page explains what Terraform manages, how the code is laid out,
and the few things you do by hand: the one-time setup, reading the deployment from your machine,
and tearing everything down.

> Naming and contracts (resource names, env vars, output keys) are defined in
> [`.kiro/specs/strands-poc/design.md`](../.kiro/specs/strands-poc/design.md) §1. This page uses `<env>`
> for `dev` or `prod`. Only `prod` is deployed (account `438750401181`, region `us-east-1`).

## 1. The three Terraform stacks

A "stack" (Terraform calls it a *root module*) is a folder you run `terraform apply` in. Each has its own
state file: Terraform's record of what it created. There are three, applied in this order and
destroyed in the reverse order:

| # | Folder | What it creates | State kept in | How often |
|---|---|---|---|---|
| 1 | `infra/terraform/bootstrap/` | The S3 bucket `shopassist-tfstate-<account>-<region>` that holds the other stacks' state (private, encrypted, TLS only, no versioning) | a local file `bootstrap/terraform.tfstate` (git-ignored) | once per account |
| 2 | `infra/terraform/platform/` | 5 ECR repositories (`shopassist-agent`, `-support-mcp`, `-orders-lambda`, `-role-mapper`, `-chat-relay`), the GitHub OIDC provider, the CI roles `shopassist-gha-plan` and `shopassist-gha-apply-<env>`, a permissions boundary per env | S3, key `platform.tfstate` | once, then rarely |
| 3 | `infra/terraform/` (the **main** stack) | The application: tables, bucket + skills, Cognito, Lambdas, runtimes, Gateway, Cedar policies | S3, key `main/<env>.tfstate` | every deploy (by the pipeline) |

Why split it up?
- The state bucket must exist before any stack can store state in it, so it can't live in those stacks.
- The platform pieces (ECR, CI roles) are shared by every environment and must survive when an
  environment is destroyed, because the pipeline needs them to deploy it again. They cost nothing while empty.
- The main stack can then be created and destroyed on demand, per environment, without touching the rest.

The S3 backend uses `use_lockfile = true`: Terraform writes a small `.tflock` file next to the state while
it runs, so two runs can't change the same stack at the same time.

## 2. The main stack in detail

### Files

| File | Role |
|---|---|
| `versions.tf` | Terraform ≥ 1.10 and provider versions (`hashicorp/aws` ≥ 6.71, needed for AgentCore resources) and the S3 backend block (filled in by `envs/<env>.backend.hcl`) |
| `providers.tf` | The AWS provider, with `default_tags` (`project=shopassist`, `environment=<env>`) added to every taggable resource |
| `variables.tf` | Every input, with validation (for example `image_tag` must be 40 hex characters) |
| `main.tf` | Wires the modules together (see below) and writes `.deploy/<env>/*.json` |
| `outputs.tf` | Values printed after apply (`terraform output`) |
| `envs/<env>.tfvars` | The environment's settings (committed, no secrets) |
| `envs/<env>.backend.hcl` | Where the environment's state lives (bucket + key) |

### Modules, in the order Terraform builds them

Terraform works out the order from the references between resources. Roughly:

| Module | Creates |
|---|---|
| `data` | S3 bucket `shopassist-<env>-artifacts-…` with every file of `skills/` under `skills/`; DynamoDB tables `orders`, `returns`, `tickets`; then runs `scripts/seed_data.py` to load the demo orders |
| `role_mapper` | Only when `enable_entra = true`: the Lambda that turns Entra roles into Cognito groups at sign-in ([12-entra-federation.md](12-entra-federation.md)) |
| `identity` | Cognito user pool, domain `shopassist-<env>-<account>`, app clients (users, machine-to-machine), groups `Group-ShopAssist-Admin` / `-Viewer`, demo users alice/bob/carol with random passwords, the AgentCore Identity OAuth2 credential provider; optionally the Entra ID identity provider |
| `lambda_tools` | The `orders` tools Lambda (container image) and its role and log group |
| `mcp_runtime` | The `support` MCP server on AgentCore Runtime (container image) |
| `gateway` | Cedar policy engine, the Gateway, its targets `orders` and `support`, one policy per `infra/policies/*.cedar` |
| `terraform_data.sync_gateway_targets` | Asks the Gateway to re-read the `support` server's tool list whenever its image changes (`scripts/sync_gateway_target.py`) |
| `agent_runtime` | The agent on AgentCore Runtime (container image), with the JWT check that only lets `Group-ShopAssist-*` users in |
| `chat_site` | The hosted chat page: private S3 bucket `shopassist-<env>-chat-ui-…`, CloudFront distribution (default `*.cloudfront.net` address), the page files and `config.json` |
| `chat_relay` | The Lambda (container image) and function URL the hosted page posts to, with a reserved-concurrency cap (`chat_relay_reserved_concurrency`, default 5) |
| `aws_budgets_budget.monthly` | Optional (`enable_budget = true`): a monthly budget with e-mail alerts |
| `local_file` / `local_sensitive_file` | `.deploy/<env>/outputs.json` (addresses and ids) and `.deploy/<env>/users.json` (demo passwords), on the machine that ran Terraform |

### Variables you'll actually touch

| Variable | Where it's set | Meaning |
|---|---|---|
| `environment` | `envs/<env>.tfvars` | `dev` or `prod`. Part of every name |
| `image_tag` | `-var image_tag=<sha>` (by the pipeline) | The full git SHA of the images to run. All five images share it. **Changing it is how a new version is rolled out** |
| `model_id` | `envs/<env>.tfvars` | The Bedrock model for the agent (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) |
| `log_retention_days`, `seed_data`, `enable_budget`, `budget_email`, `budget_limit_usd` | `envs/<env>.tfvars` | Self-explanatory; see `variables.tf` |
| `enable_entra`, `entra_*` | `envs/<env>.tfvars`; the client secret only as `TF_VAR_entra_client_secret` from a GitHub environment secret | Entra ID sign-in, off by default |

Image URIs are not variables: the main stack reads the ECR repository URLs from the platform stack's state
(`data.terraform_remote_state.platform`) and adds `:<image_tag>`.

### How a rollout works

1. The pipeline pushes `shopassist-agent:<sha>` (and the other three images) to ECR.
2. It runs `terraform plan -var-file=envs/prod.tfvars -var image_tag=<sha>`. Terraform sees that the runtimes'
   and Lambdas' image URI changed and plans an **in-place update** (same runtime id, new version).
3. After approval, `terraform apply` makes the change. The Gateway re-syncs the `support` tool list.

A rollback is the same thing with an older SHA. ECR keeps the current and the previous image of each repository.

## 3. One-time setup (already done for this account)

You only redo this in a new account, or after `scripts/destroy_everything.sh`. Use the `personal` profile.

```bash
# 1. State bucket (local state)
cd infra/terraform/bootstrap
AWS_PROFILE=personal terraform init
AWS_PROFILE=personal terraform apply

# 2. Platform: ECR, GitHub OIDC, CI roles (state in the bucket from step 1)
cd ../platform
AWS_PROFILE=personal terraform init -backend-config=backend.hcl
AWS_PROFILE=personal terraform apply
AWS_PROFILE=personal terraform output github_variables_commands   # the `gh variable set` commands
```

3. In GitHub, set the repository variables (`AWS_REGION`, `AWS_PLAN_ROLE`, `DEPLOY_DEV=false`), create the
   environments `prod` (required reviewer, branch `main` only, variable `AWS_APPLY_ROLE`) and `dev`. The
   output above prints the exact commands. Details: [10-ci-cd.md §5](10-ci-cd.md#5-github-settings).
4. Deploy through GitHub: **Actions → deploy → Run workflow**.

Platform before the first deploy that includes the hosted chat page: it adds the ECR repository
`shopassist-chat-relay` and the apply role's third policy `shopassist-gha-ui-<env>`. Apply the platform stack once
(`AWS_PROFILE=personal terraform apply` in `infra/terraform/platform`) before running `deploy.yml`, or the image push
and the CloudFront calls fail. The deploy then prints no page address; read `chat_ui_url` from `.deploy/<env>/outputs.json`.

## 4. Getting `.deploy/<env>/` on your machine

The CLI scripts (`invoke_agent.py`, `demo_access.py`, `chat_ui.py`) read `.deploy/<env>/outputs.json` and
`.deploy/<env>/users.json`. Terraform writes these on the machine that runs `apply`, which for a pipeline
deploy is the GitHub runner (the files are deleted there at the end of the job). To recreate them locally,
apply **only** those two files against the live state, with the SHA that is currently deployed:

```bash
cd infra/terraform
AWS_PROFILE=personal terraform init -reconfigure -backend-config=envs/prod.backend.hcl
AWS_PROFILE=personal terraform plan -var-file=envs/prod.tfvars -var image_tag=<deployed sha> \
  -target=local_file.deploy_outputs -target=local_sensitive_file.demo_users -out local.tfplan
# Check the plan: it must list ONLY local_file.deploy_outputs and local_sensitive_file.demo_users.
AWS_PROFILE=personal terraform apply local.tfplan
```

The deployed SHA is shown in the last deploy run's summary. If the plan lists anything else, don't apply
it: the SHA or your working copy differs from what is deployed. The scripts pick the environment from
`SHOPASSIST_ENV` (default `prod`).

> Not yet run end to end; added 2026-11-08. `.deploy/` is git-ignored because `users.json` holds passwords.

## 5. Smoke test from your machine

```bash
uv run python scripts/invoke_agent.py --user alice --prompt "Where is order 1042?"
uv run python scripts/invoke_agent.py --user bob   --prompt "I'm C001. Please return the headphones from order 1042."
uv run python scripts/invoke_agent.py --user carol --prompt "Hello"      # expect HTTP 401/403
uv run python scripts/demo_access.py                                    # the full access matrix
```

| # | Scenario | Pass condition | Enforced by |
|---|---|---|---|
| a | alice asks to return the 1042 headphones | `orders___create_return` used | all layers allow |
| b | bob asks the same | no `create_return`, the reply mentions permission | Cedar hides the tool, the skill explains |
| c | carol calls the agent | HTTP 401/403 | Runtime JWT authorizer (`cognito:groups` claim) |
| d | Direct Gateway calls, no agent | bob's `tools/list` lacks the 2 write tools and his `create_return` call is denied; alice sees all 7 | AgentCore Policy (Cedar) |

`demo_access.py` re-seeds the orders table first (unless `--no-reseed`), exits non-zero on any unexpected
result, and accepts `--only b,d` and `--timeout`. The pipeline runs `--no-reseed --only b,c,d` after each
deploy (scenario a writes data, so it's left for manual runs). The first call of a session is slower
(cold start).

## 6. What changes when

| You change | Then | Effect |
|---|---|---|
| Agent, support server, orders Lambda or role-mapper code | merge, then run `deploy.yml` | New images for that SHA; in-place runtime/Lambda updates |
| A skill (`skills/**`) | merge, then run `deploy.yml` | Only the changed S3 objects. Skills load once per session, so start a new session (`--new-session`) |
| `tools_schema.json` | merge + deploy | The `orders` target's tool list is updated |
| A Cedar file (`infra/policies/*.cedar`) | merge + deploy | The policy is updated in place; a new file becomes a new policy |
| `envs/prod.tfvars` (model, retention, budget, Entra) | merge + deploy | Whatever the plan shows; read it before approving |
| Demo data only | `AWS_PROFILE=personal uv run python scripts/seed_data.py --region us-east-1 --prefix shopassist-prod` | Resets orders and return windows |

## 7. Destroy

| Goal | How | Removes |
|---|---|---|
| Take one environment down | GitHub **Actions → destroy** (typed confirmation + approval) | Everything in the main stack, plus images, secrets in their recovery window, stray log groups and the env state file. Keeps the free foundation (state bucket, empty ECR repos, OIDC, CI roles) so `deploy.yml` can recreate it |
| Remove everything | `AWS_PROFILE=personal bash scripts/destroy_everything.sh` (asks you to type `destroy everything`) | Every env, then platform, then bootstrap, then an account-wide leftover scan |
| Check for leftovers | `AWS_PROFILE=personal uv run python scripts/leftovers.py --region us-east-1 --env prod` (or `--all`) | Nothing: it lists anything named or tagged `shopassist` and exits 1 if something is left |

Both destroy paths end with `scripts/leftovers.py`, which checks every service by name and by tag
(`project=shopassist`) and confirms tagged resources really still exist (AWS's tag index can lag for a while).

## 8. Running Terraform locally (rarely needed)

Everything the pipeline does can be done by hand with the `personal` profile, for debugging:

```bash
cd infra/terraform
AWS_PROFILE=personal terraform init -reconfigure -backend-config=envs/prod.backend.hcl
AWS_PROFILE=personal terraform plan -var-file=envs/prod.tfvars -var image_tag=<sha>
```

Don't `apply` from your machine unless the pipeline is unavailable: the pipeline is the reviewed, approved
path, and a local apply also needs the images for `<sha>` to exist in ECR already. To check that dev still
plans: `terraform init -reconfigure -backend-config=envs/dev.backend.hcl` and `plan -var-file=envs/dev.tfvars`.

## 9. Verified runs

| Date | What | Result |
|---|---|---|
| 2026-11-07 | bootstrap + platform applied locally | ✅ buckets, repos, roles, OIDC trust |
| 2026-11-07 | `plan` for prod and dev with a placeholder SHA | ✅ 53 to add each, nothing applied |
| 2026-11-07 | First prod deploy through `deploy.yml` | ✅ 53 added; smoke c, d ✅, b fixed in the skill and ✅ on 2026-11-08 |
| 2026-11-08 | `destroy.yml` for prod | ✅ 51 destroyed, sweep + scan CLEAN |

Open items (second deploy, rollback, negative role checks, redeploy after destroy) are tracked in
[`.kiro/specs/prod-deploy/prod-deploy-tasks.md`](../.kiro/specs/prod-deploy/prod-deploy-tasks.md).

## History

Until 2026-11-06 the stack could also be deployed with a boto3 script (`infra/boto3/`), and the code shipped as
zip files built by `scripts/build.py`. Both were removed: Terraform is the only deploy path, and every workload is a
container image. The reasons are in [`.kiro/steering/decisions.md`](../.kiro/steering/decisions.md).
