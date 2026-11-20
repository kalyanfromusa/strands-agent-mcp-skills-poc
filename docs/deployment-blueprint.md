# Deployment blueprint (reusable)

A short description of how ShopAssist is delivered, written so the same setup can be reused for another app,
including one with mobile clients. The full reasoning is in `.kiro/specs/prod-deploy/prod-deploy-pipeline.md`.

## 1. Principles

| Principle | How it's done here |
|---|---|
| One deploy path | **Terraform only**. Image rollouts are also Terraform (`-var image_tag=<sha>`), no side-channel API calls, no `ignore_changes` |
| Immutable artifacts | Every workload is an **arm64 container image** tagged with the **full git SHA**; ECR tags are immutable, scan on push |
| Build once, promote | The same image SHA goes to dev and prod; rollback = deploy an older SHA |
| No stored cloud keys | GitHub Actions → **OIDC** → IAM roles. Only a job in a protected GitHub environment can assume the apply role |
| Human gate | Deploy is **manual** (`workflow_dispatch`) with a required reviewer on the `prod` environment |
| Public-repo safe logs | Plans/applies print only "address: action" summaries; full output never reaches logs |
| Create/destroy on demand | Each environment can be destroyed completely by a workflow and recreated by the deploy workflow |
| Cheap when idle | Serverless/on-demand services only (Lambda, DynamoDB on-demand, AgentCore, S3); ECR keeps 2 images per repo |

## 2. Terraform layout (three roots)

| Root | Contains | State | Lifetime |
|---|---|---|---|
| `bootstrap/` | S3 state bucket (encrypted, public access blocked) | local, git-ignored | once per account |
| `platform/` | ECR repos, GitHub OIDC provider, CI roles (`<app>-gha-plan`, `<app>-gha-apply-<env>`) | S3, `use_lockfile = true` | shared by all envs, free when idle |
| main (per env) | the application stack, built from `modules/*` | S3, key `main/<env>.tfstate` | created/destroyed on demand |

- Per env: `envs/<env>.tfvars` (committed, no secrets) + `envs/<env>.backend.hcl` (state key).
- Names follow `<app>-<env>-*`, so environments can share one account.
- Secrets enter only as `TF_VAR_*` from GitHub environment secrets.
- Teardown order: main → platform → bootstrap.

## 3. GitHub Actions workflows

| Workflow | Trigger | What it does | AWS role |
|---|---|---|---|
| `ci.yml` | pull request | tests, `terraform fmt/validate`, tflint, checkov, image builds (no push), **summary-only plan** posted as a PR comment (same-repo PRs only) | plan (read-only) |
| `build.yml` | merge to `main` | tests, build + Trivy scan images, **no push, no deploy**; prints the SHA ready to deploy | none |
| `deploy.yml` | manual (`target_env`, `image_tag`) | test → plan → **approval** → build+scan+push (only images missing in ECR) → re-plan + hash check → apply → smoke test | plan, then apply-`<env>` |
| `destroy.yml` | manual, typed confirmation | approval, plan-hash guard, destroys the env (data, secrets, users, logs, images, state), then scans for leftovers | apply-`<env>` |
| `_checks.yml` | reusable | shared test, IaC and image check jobs | none |

Hygiene: actions pinned to SHAs, minimal `permissions` (`id-token: write`, `contents: read`),
`persist-credentials: false`, a `concurrency` group that never cancels an apply, `image_tag` validated as 40 hex chars,
no plan file is uploaded: the apply job re-plans and refuses to apply if the plan hash differs from the approved one.

## 4. Identity and access

- **Cognito user pool** is the single token issuer for the backend. Native users and **Entra ID** (OIDC federation)
  users both end up with the same Cognito groups.
- Group chain: `AD-<App>-<Env>-<Role>s` (people) → Entra app role `AppRole-<App>-<Role>` → Cognito group
  `Group-<App>-<Role>`, translated by a **pre-token-generation Lambda** with an allow-list map (fail closed).
- Backend checks the JWT (`cognito:groups`) at the entry point; fine-grained rules live in policy (Cedar here).
- Machine-to-machine calls use Cognito **client-credentials** tokens.

## 5. Reusing it for an app with mobile clients

Keep sections 1–4 as they are. Add the following.

**Backend**
- Replace/extend the AgentCore pieces with what the app needs (e.g. API Gateway HTTP API + Lambda images, or ECS
  Fargate), still as SHA-tagged images rolled out by Terraform. Use a **Cognito JWT authorizer** on the API.
- Add a mobile **Cognito app client per platform/env**: public client (no secret), authorization code + **PKCE**,
  redirect URIs as custom schemes or universal/app links (e.g. `myapp://callback`, `https://app.example.com/callback`),
  short access-token lifetime, refresh-token rotation on. Tokens stored in Keychain / Android Keystore.
- If the app needs push notifications: SNS / AWS End User Messaging platform applications (APNs key, FCM
  credentials as environment secrets → `TF_VAR_*`).
- Emit a per-env **mobile config** output from Terraform (API URL, user pool id, client id, Cognito domain) that the
  mobile build reads, the same way `.deploy/<env>/outputs.json` is written here. Nothing secret goes into the app.

**Mobile pipeline** (separate workflows, same gates)

| Workflow | Trigger | What it does |
|---|---|---|
| `mobile-ci.yml` | pull request | lint, unit tests, debug build (Android on `ubuntu`, iOS on `macos` runners) |
| `mobile-build.yml` | merge to `main` | signed release builds tagged with the git SHA; upload as artifacts (no store upload) |
| `mobile-release.yml` | manual (`target_env`, `build_sha`) | approval on the env → upload to TestFlight / Play internal track (Fastlane or EAS) → promote manually in the stores |

- Signing material (iOS certificates/profiles or App Store Connect API key, Android keystore, Play service-account
  JSON) lives only in **GitHub environment secrets**, same as the backend secrets.
- Version: semantic version for users + build number from `github.run_number`; record the backend SHA it was
  tested against.
- Order of release: deploy the backend first and keep APIs **backward compatible** (old app versions stay installed
  for months); add a minimum-version check endpoint for forced upgrades.
- Mobile store artifacts aren't destroyable like infra; `destroy.yml` only covers the backend.

## 6. Checklist for a new app

1. Pick names: `<app>`, environments, AWS account, GitHub repo.
2. Apply `bootstrap/` then `platform/` locally (once), with an admin profile.
3. Create GitHub environments `dev`/`prod` (prod: required reviewer, `main` only) and their secrets/variables.
4. Write `modules/*` and the main root with `environment` + `image_tag` variables; `envs/<env>.tfvars`.
5. Add Dockerfiles (arm64, deps pinned from the lockfile) and the four workflows above.
6. First deploy through `deploy.yml`; verify with the smoke test; test `destroy.yml` + redeploy once.
7. Mobile: add the app clients, config output and mobile workflows; ship to TestFlight / Play internal first.

## 7. Lessons from this project
- Least-privilege apply roles take a few iterations; scope by name prefix (`<app>-<env>-*`) and limit `iam:PassRole`.
- Image scans: base images need OS security updates in the Dockerfile; document any accepted exceptions explicitly.
- AWS's tag index (Resource Groups Tagging API) can lag after deletes; a leftover scan must confirm each tagged ARN really exists.
- A scan step piped through `| tee` hides its exit code; use `set -o pipefail`.
- Keep the destroy workflow honest: run a leftover scan by tag and name prefix after every destroy.
