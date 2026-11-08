# Prod deploy: tasks

The work queue for [`prod-deploy-pipeline.md`](prod-deploy-pipeline.md) (approved 2026-11-06). Work top to bottom.
Same workflow as `.kiro/steering/agent-workflow.md`: pick the first unchecked task, implement it, verify it, tick it.

**Markers**
- 🔑 uses the AWS profile `personal` (account 438750401181), passed per command (`--profile personal` /
  `AWS_PROFILE=personal`) to access and deploy the app. **Never used to run the Claude session itself**, and never as a
  fallback if the session's token expires. Destructive steps (P1.2 teardown, any destroy) still get a confirmation first.
- 👤 a step only you can do (Entra portal, ngrok signup, GitHub settings, approvals). Claude writes the runbook,
  you do the step, then tell Claude.
- 🧪 verification. A task is ticked only when its check passes.

Nothing goes in git: secrets, `.deploy/`, `*.tfstate*`, `tfplan`, `dist/`, Entra secrets, the ngrok authtoken.

---

## P0. Contracts first
- [x] 0.1 `design.md`: env-aware naming `shopassist-<env>-*` / `shopassist_<env>_*` (§1); images instead of zips (§5);
      the 3 Terraform roots and state layout; `Group-ShopAssist-Admin` / `Group-ShopAssist-Viewer` (§8); Entra chain
      (`AD-` → `AppRole-` → `Group-`, `entra_role_source`, `entra_role_map`); the `onprem` target, its tools and the
      `shopassist-onprem` client + `onprem/read` scope; new env vars and variables (`environment`, `image_tag`,
      `onprem_api_base_url`)
- [x] 0.2 `decisions.md`: dated entries that supersede boto3, zip deploy and GitHub-only image rollout; Terraform-only;
      GitHub Actions with manual deploy; Entra app roles; ngrok; dev + prod defined, prod deployed
- [x] 0.3 `tech.md`, `structure.md`, `product.md` (if needed), `CLAUDE.md` pointer to this spec
- [x] 0.4 🧪 Review: no contract left that still names boto3, zips, `shop-admin` or `shopassist_agent` (without env)
      *(done 2026-11-06: the remaining hits are the superseded history entries in `decisions.md` only; `requirements.md` updated too)*

## P1. Tear down the boto3 stack (account 438750401181)
- [x] 1.1 🔑 Read-only inventory just before teardown (runtimes, gateway, policy engine, credential provider, Lambda,
      tables, bucket, pool, roles, log groups, budget), saved to the task notes
      *(2026-11-06: runtimes `shopassist_agent-PC8kasGY4u`, `shopassist_support_mcp-BhHqPjEIHl` (READY, DEFAULT endpoints);
      gateway `shopassist-gateway-ubbucsbprn` with targets `orders`, `support`; policy engine
      `shopassist_policy_engine-zag0kjlleh`; OAuth provider `shopassist-cognito`; 3 workload identities (2 runtimes +
      gateway); Lambda `shopassist-orders-tools`; tables orders (10 items), returns (1), tickets (0); bucket
      `shopassist-artifacts-438750401181-us-east-1` (7 objects, 54 MB); pool `us-east-1_esTTDtTkW`; 4 IAM roles; 3 log
      groups; budget `shopassist-monthly`. Not ours, keep: budget "My Monthly Cost Budget", ECR `cdk-hnb659fds-*`.
      Account month-to-date cost $4.29.)*
- [x] 1.2 🔑 `AWS_PROFILE=personal uv run python infra/boto3/destroy.py --yes` *(2026-11-06: all 15 steps ✓, 85.5 s)*
- [x] 1.3 🔑 🧪 Leftover checks by name and tag (`project=shopassist`, log groups with `MSYS_NO_PATHCONV=1`, IAM roles,
      Cognito, S3, budget) are empty. The unrelated `cdk-hnb659fds-*` ECR repo is untouched
      *(2026-11-06: every name check is empty, including workload identities and secrets. The tagging API still listed the
      deleted pool (an eventually-consistent index); `DescribeUserPool` returns ResourceNotFound. Kept: "My Monthly
      Cost Budget", the CDK ECR repo.)*
- [x] 1.4 Clear the stale `.deploy/outputs.json`, `users.json` and `session-*.txt`

## P2. Remove the boto3 path, zips and the runtime-update script
- [x] 2.1 Delete `infra/boto3/`, `tests/test_boto3_shapes.py`
- [x] 2.2 Delete `scripts/build.py`, `tests/test_build.py`, `scripts/ci/update_runtime_image.py`,
      `tests/test_ci_update_runtime.py`, `docs/ci-cd-integration-patch.md`
- [x] 2.3 Remove boto3/zip references in code comments, `pyproject.toml` and `.gitignore` (keep ignoring `dist/`)
- [x] 2.4 🧪 `uv run pytest -q` green; `grep -ri boto3.deploy\|build.py\|update_runtime_image` only hits docs that P10 rewrites
      *(2026-11-06: 151 tests pass. Remaining hits are `docs/*`, README (P10), `infra/terraform/{main.tf,modules/data}` and
      `infra/terraform/ci/iam.tf` (rewritten in P3/P5) and `.github/workflows/deploy.yml` (rewritten in P6; until then it's
      inert, because `AWS_ROLE_ARN` is unset). Local `dist/` and `build/` (old zips and logs) deleted.)*

## P3. Terraform foundations
- [x] 3.1 `infra/terraform/bootstrap/`: S3 state bucket (**no versioning**: Suspended, by decision 2026-11-06; SSE,
      public access blocked, TLS-only policy, `force_destroy` for cost hygiene), local state, git-ignored
- [x] 3.2 `infra/terraform/platform/` (replaces `ci/`): 4 ECR repos (immutable tags, scan on push, lifecycle **keep 2 = current + previous**,
      `force_delete`), the GitHub OIDC provider, `shopassist-gha-plan` (read-only + state access; trust: same-repo
      `pull_request` + `ref:refs/heads/main`), `shopassist-gha-apply-prod` / `-dev` (least privilege + permissions
      boundary; trust: `environment:<env>` only), S3 backend `platform.tfstate` *(applied 2026-11-07: 20 resources)*
- [x] 3.3 Delete `infra/terraform/ci/`
- [x] 3.4 🧪 `fmt`, `validate`, `checkov` locally *(2026-11-06: valid; checkov 0 failed, 12 skipped with reasons in code.
      tflint moves to CI, because there's no local Docker)*
- [x] 3.5 🔑 `terraform apply` bootstrap ✅ (2026-11-07), then platform ✅ (2026-11-07). 🧪 The buckets, repos and roles exist, and the trust
      policies match the plan (checked with `aws iam get-role`)
      *(verified: 4 repos IMMUTABLE, lifecycle "keep 2"; plan trust = main + pull_request; apply-prod = environment:prod only;
      apply-dev = environment:dev only)*

## P4. Container images (built in GitHub Actions only; no local Docker, decided 2026-11-07)
- [x] 4.1 Pinned requirements exported from `uv.lock` per image (a script plus a test that fails if they drift) *(2026-11-07: `scripts/pin_requirements.py` (+ `--check`), `requirements.in` → `requirements.lock.txt` with hashes; `tests/test_pinned_requirements.py`)*
- [x] 4.2 `mcp_servers/orders_lambda/Dockerfile` (`public.ecr.aws/lambda/python:3.12-arm64`, handler unchanged) *(2026-11-07, with an allow-list `.dockerignore`)*
- [x] 4.3 Agent and support Dockerfiles: use the pinned requirements; keep the non-root user and the allow-list context *(2026-11-07: `--require-hashes` from the lock files; comments updated for Terraform-only)*
- [x] 4.4 Container checks as a CI script (`scripts/ci_image_checks.sh`), run on the arm64 runner after each build: agent
      `/ping`, support `/mcp` `tools/list`, Lambda RIE invoke of `get_order`; Trivy fails on HIGH/CRITICAL (or each is
      documented)
      *(script written 2026-11-07: `scripts/ci_image_checks.sh`; it can only run on the arm64 runner, so it is verified in 6.7; ✅ passed in PR #1)*

## P5. Main stack: containers, environments (code + offline checks; the first deploy runs through the pipeline in P6)
- [x] 5.1 Variables `environment`, `image_tag` (40-hex validation), image URIs from platform outputs; env-aware names *(2026-11-07: images = platform ECR URL + `:<image_tag>`, read via `terraform_remote_state`)*
- [x] 5.2 `modules/agent_runtime`, `modules/mcp_runtime` → `container_configuration`; `modules/lambda_tools` →
      `package_type = "Image"`; `modules/data` uploads skills only, and the artifacts bucket has **no versioning** *(2026-11-07)*
- [x] 5.3 Rename the groups to `Group-ShopAssist-Admin` / `-Viewer`: Cognito, Runtime `allowed_groups`, Cedar files,
      demo users, `local_gateway.py --role`, tests *(2026-11-07; docs follow in P10)*
- [x] 5.4 Gateway target sync as `terraform_data` + `local-exec` when the support/onprem runtime image changes *(2026-11-07: `scripts/sync_gateway_target.py`, `terraform_data.sync_gateway_targets`)*
- [x] 5.5 S3 backend (`use_lockfile`), `envs/{dev,prod}.tfvars`, `envs/{dev,prod}.backend.hcl`; every IAM role carries
      the env's permissions boundary *(2026-11-07; the CLI reads `.deploy/$SHOPASSIST_ENV/`, default prod)*
- [x] 5.6 🧪 `uv run pytest`; `validate`; local run (`run_local.py` against `local_gateway.py --port 8765`) still works
      *(2026-11-07: pytest 155 ✅, validate ✅. Local run ✅ with Claude Haiku 4.5 (switched because the Haiku 5.5 agreement isn't
      accepted in 438750401181): viewer → eligibility checked, `create_return` refused with a permission explanation. Note:
      it then offered to open a ticket, which a viewer can't do either; tighten the skill text in P8/P10)*
- [x] 5.7 🔑 🧪 `terraform plan` for prod and dev succeeds with a placeholder SHA (**not applied**) *(2026-11-07: prod 53 to add, dev 53 to add, 0 change/destroy)*

## P6. GitHub Actions + first deploy
- [x] 6.1 `ci.yml` (PRs): pytest, IaC checks (tflint, checkov), arm64 image builds + container checks without push,
      **summary-only** plan comment (same-repo PRs, skipped for forks)
- [x] 6.2 `build.yml` (push to `main`): tests + build/check/scan, **no push, no deploy**
- [x] 6.3 `deploy.yml` (manual only): inputs `target_env`, `image_tag`; test → plan (encrypted artifact, summary only) →
      `environment: <env>` approval → build + push if the SHA is missing → apply of that plan → smoke; on failure delete the
      images this run pushed; `DEPLOY_DEV=false` blocks dev
- [x] 6.4 Log hygiene: no full plan/apply output, `::add-mask::`, SHA-pinned actions, minimal `permissions`,
      `concurrency` with no cancel-in-progress
- [x] 6.5 🧪 `actionlint` clean *(2026-11-07. Files: `_checks.yml` (reusable: tests, lock check, fmt/validate/tflint/checkov,
      arm64 build+check+Trivy), `ci.yml`, `build.yml`, `deploy.yml`, `.github/actions/build-image`, `scripts/tf_plan_summary.py`
      (+ tests). Design change: no plan artifact and no secrets needed. The approved job re-plans and applies only if the
      plan hash equals the approved one; the smoke test uses the users file Terraform just wrote. `DEPLOY_USERS_JSON`
      and the plan-encryption key are dropped.)*
- [ ] 6.6 👤 GitHub settings *(environments, variables and ruleset done 2026-11-07; the ruleset still needs: required approvals 0, "Restrict updates" off or yourself as bypass, code-coverage rule removed)*: environments `prod` (required reviewer = you, branch `main`) and `dev`; variables (role
      ARNs, region, `DEPLOY_DEV=false`); environment secrets (none yet; `TF_VAR_entra_client_secret` in P7); branch protection on `main`; deployment branch `main` only on `prod`; 2FA on your account; no `pull_request_target` workflows. Claude prints the exact `gh` commands
- [x] 6.7 🧪 Open a PR → the images build and pass the checks, the summary plan comment appears, nothing is applied
      *(2026-11-07, PR #1: tests, iac, 3 images (build + container checks + Trivy) and plan all pass; the comment shows
      "53 to add" with addresses only. Fixes found along the way: Debian security updates in the slim images, a
      documented Trivy exception for the Lambda RIE (expires 2026-12-31), and CI trust policies switched to GitHub's
      immutable OIDC subject `repo:kalyanfromusa@28615456/strands-agent-mcp-skills-poc@1438682520:*`)*
- [x] 6.8 🧪 Merge → `build.yml` builds and scans, **nothing is pushed or deployed** *(2026-11-07: run on 9567c7d green, ECR still empty afterwards)*
- [ ] 6.9 🧪 **First prod deploy**: manual `deploy.yml` (prod) → 👤 approve → images pushed, apply + smoke green
      *(2026-11-07 run 42425427780: images pushed, **apply 53 added** ✅; smoke c, d ✅ (Runtime 401, Cedar filters + denies);
      b ❌ only on wording: Haiku 4.5 didn't say "no permission". Skill step 6 fixed; re-deploy pending)*
- [ ] 6.10 🔑 🧪 `invoke_agent.py --user alice "Where is my order 1042?"`; `demo_access.py` a–d all ✅
- [ ] 6.11 🧪 Second deploy with a new SHA → in-place runtime update (not a replacement); ECR holds 2 images per repo
- [ ] 6.12 🧪 Rollback: `deploy.yml` with the previous SHA → the old image is live again
- [ ] 6.13 🔑 🧪 Negative checks: the plan role can't write (an `AccessDenied` on a test call); a run without the `prod`
      environment can't assume the apply role

## P6b. Create / destroy on demand (requested 2026-11-07)
- [x] 6b.1 `scripts/leftovers.py`: scans every service by name and tag for one env (or `--all`); `--sweep` removes ECR images,
      AgentCore Identity secrets in their recovery window, stray log groups and the env state file; exit 1 if anything is left
      *(2026-11-07: dev → CLEAN; prod → lists every deployed resource correctly)*
- [x] 6b.2 `.github/workflows/destroy.yml`: typed confirmation, plan -destroy summary, approval, re-plan hash guard, destroy, sweep,
      final scan *(actionlint clean)*
- [x] 6b.3 Platform: second apply-role policy `shopassist-gha-sweep-<env>` (6,144-char managed-policy limit) *(applied 2026-11-07)*
- [x] 6b.4 `scripts/destroy_everything.sh`: local full teardown (envs → platform → bootstrap → `leftovers.py --all`), typed confirmation
- [ ] 6b.5 🧪 Destroy prod with `destroy.yml` → leftover scan CLEAN; then `deploy.yml` → prod is back and smoke green
- [ ] 6b.6 🧪 (when the demo is over) `destroy_everything.sh` → `leftovers.py --all` CLEAN

## P7. Entra ID federation (cloud-only groups, app roles)
- [ ] 7.1 Write `docs/12-entra-federation.md` runbook part 1 (tenant, P2 trial, app registration `ShopAssist-Prod`,
      app roles `AppRole-ShopAssist-Admin` / `-Viewer`, groups `AD-ShopAssist-Prod-Admins` / `-Viewers`, test users
      for admin/viewer/none, assignment required, client secret, token configuration)
- [ ] 7.2 👤 Do the Entra steps; give Claude the tenant id, client id and group/role names (the secret goes only to the
      GitHub environment secret and a local git-ignored tfvars)
- [ ] 7.2b Custom sign-in domain `strands-agents-poc.nalamone.com`: an ACM certificate in us-east-1 (DNS validation CNAME added
      in Cloudflare by 👤 you), Cognito custom domain, and a CNAME in Cloudflare to Cognito's CloudFront endpoint (DNS only,
      not proxied). Note: Cognito requires the parent domain `nalamone.com` to have an A record
- [ ] 7.3 Terraform: Cognito OIDC IdP `EntraID`, custom attribute `custom:entra_roles`, users client with auth-code +
      PKCE, `COGNITO` + `EntraID`, loopback callbacks, access token 15 min / refresh 8 h
- [ ] 7.4 Role-mapper Lambda (container image, 4th ECR repo or a shared one): `entra_role_source`, `entra_role_map`,
      allow-list, fail-closed, highest role wins, logs decisions without tokens; unit tests
- [ ] 7.5 🧪 Verify `groupOverrideDetails` (V1) reaches the **access** token. If not, switch to V2 and note the Essentials cost
- [ ] 7.6 `invoke_agent.py --login` and the chat UI "Sign in" button (PKCE, loopback, state + nonce checks); tests
- [ ] 7.7 🔑 Deploy via `deploy.yml` (👤 approve)
- [ ] 7.8 🧪 Demo e1 (admin → all tools), e2 (viewer → read-only), e3 (no group → blocked); alice/bob/carol unchanged
- [ ] 7.9 🧪 Revocation: move the e1 user to Viewers → after re-sign-in `create_return` is gone

## P8. Simulated on-prem API (Cloudflare Tunnel at onprem-prod.nalamone.com; decided 2026-11-07, replaces ngrok)
- [ ] 8.1 `onprem_api/` FastAPI warehouse API (`GET /stock/{sku}`, `GET /restock-eta/{sku}`), its own seed data,
      OpenAPI spec, binds `127.0.0.1` (pick a free port, not 8000)
- [ ] 8.2 Security: JWT validation (JWKS, `iss`, `token_use`, `client_id`, scope `onprem/read`, `exp`), rate limit,
      size limits, audit log, no stack traces; unit tests including negative cases
- [ ] 8.3 👤 Cloudflare Tunnel: install `cloudflared`, `cloudflared tunnel login`, create the named tunnel `shopassist-onprem`,
      route `onprem-prod.nalamone.com` to `http://127.0.0.1:8770` (tunnel credentials stay local only). Claude writes the steps
      and the start script
- [ ] 8.4 Terraform: Cognito client `shopassist-onprem` + scope, AgentCore Identity provider, Gateway target `onprem`
      (OpenAPI, OAuth2 client credentials), `onprem_api_base_url` in `envs/prod.tfvars`, Cedar policies for the onprem
      tools (admin + viewer)
- [ ] 8.5 Skill `skills/inventory-check/SKILL.md` (`mcp-servers: "onprem"`, an offline message, never invents numbers);
      `local_gateway.py` emulation of `onprem___*`; tests
- [ ] 8.6 🔑 Deploy via `deploy.yml` (👤 approve)
- [ ] 8.7 🧪 The agent answers "Is SKU … in stock?" from AWS through the tunnel; the audit log shows the Gateway's client
- [ ] 8.8 🧪 Negative: a direct call to the ngrok URL with no or a wrong token → 401/403; with the tunnel stopped, the
      agent says the warehouse is unreachable

## P9. End-to-end
- [ ] 9.1 🔑 🧪 Full run through GitHub Actions on a fresh SHA: every scenario (a–d, e1–e3, on-prem) ✅, recorded with
      the date
- [ ] 9.2 🔑 🧪 `terraform plan -destroy` for main → platform → bootstrap covers every `project=shopassist` resource.
      Prod **stays deployed**; the real destroy runs only when you ask
- [ ] 9.3 Cost check: the current month's spend and the expected idle cost, recorded

## P10. Documentation (last)
- [ ] 10.1 README (overview, quick start, Terraform-only, the two accounts and the profile rule)
- [ ] 10.2 `docs/03-architecture.md`: new diagram (images, GitHub Actions, Entra/AD, on-prem) and request flows
- [ ] 10.3 `docs/04-deploy-terraform.md` rewritten: 3 roots, state backend, envs, `image_tag`, first deploy, destroy order
- [ ] 10.4 `docs/10-ci-cd.md` rewritten: OIDC roles, the 3 workflows, `prod` approval, secrets, log hygiene, rollback
- [ ] 10.5 `docs/12-entra-federation.md` finished: admin runbook, role mapping, `groups` mode after the trial, revocation, troubleshooting
- [ ] 10.6 `docs/13-onprem-api.md` (new): ngrok setup, security layers, offline behavior, switching to your own domain later
- [ ] 10.7 `docs/07-cost.md`, `docs/09-troubleshooting.md` (new failure modes), `docs/11-chat-ui.md` (sign-in),
      `docs/01`, `02`, `08` touch-ups
- [ ] 10.8 Remove `docs/05-deploy-boto3.md` and `docs/06-terraform-vs-boto3.md` (a short historical note in `04`); fix
      the doc numbering and links
- [ ] 10.9 Steering, `design.md`, `CLAUDE.md` and the original `strands-poc/tasks.md` (a note pointing here) are final
- [ ] 10.10 🧪 Every command in the docs was run in P5–P9; `grep` finds no links to removed files and no `shop-admin`/boto3 leftovers
- [ ] 10.11 "Verified run" section with dates and results (in `04`, `10`, `12`, `13`)
