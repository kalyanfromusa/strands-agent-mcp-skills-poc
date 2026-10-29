# 6. Terraform vs boto3

The repo deploys the same resource graph twice: declaratively with Terraform (`infra/terraform/`) and
imperatively with boto3 (`infra/boto3/`). Both write the same `.deploy/outputs.json`, so the client
scripts don't care which one you used. This page compares them from a PoC point of view. No AWS deployment has
been run yet, so this comparison is based on the code, not on measured deploy times.

## Mental model

| | Terraform | boto3 |
|---|---|---|
| You write | The desired end state (resources and their references) | The calls to get there, in order |
| Who orders the work | Terraform, from the dependency graph | You, in `deploy.py` (13 numbered steps) |
| Memory of what exists | State file (`terraform.tfstate`) | `infra/boto3/state.json` (a cache) + name lookups |
| Change detection | `plan`: diff of state + refresh vs config | Hand-written per step: compare `get_*` output with the desired request (`_differs`, `gateway_drift`, `target_differs`, MD5/SHA-256 of zips) |
| Teardown | `terraform destroy`, reverse graph | `destroy.py`, a hand-ordered reverse list |

## Same graph, two notations

| Resource | Terraform | boto3 call(s) (`infra/boto3/steps/...`) |
|---|---|---|
| Artifacts bucket | `aws_s3_bucket`, `aws_s3_bucket_versioning`, `aws_s3_bucket_public_access_block` | `create_bucket`, `put_bucket_versioning`, `put_public_access_block`, `put_bucket_tagging` (`s3.py`) |
| Zips and skills | `aws_s3_object.artifact` (etag = MD5), `aws_s3_object.skill` (`fileset`) | `head_object` + `put_object` if MD5 ≠ ETag, `delete_objects` for removed skills |
| Tables | `aws_dynamodb_table` × 3 | `create_table` + `table_exists` waiter (`dynamodb.py`) |
| Seed | `terraform_data.seed` with `local-exec` | `subprocess.run(seed_data.py)` |
| User pool, domain | `aws_cognito_user_pool`, `aws_cognito_user_pool_domain` | `create_user_pool`, `create_user_pool_domain` + poll until `ACTIVE` (`cognito.py`) |
| Resource server, clients | `aws_cognito_resource_server`, `aws_cognito_user_pool_client` × 2 | `create_resource_server`, `create_user_pool_client` / `update_user_pool_client` |
| Groups, users | `aws_cognito_user_group`, `random_password`, `aws_cognito_user`, `aws_cognito_user_in_group` | `create_group`, `admin_create_user`, `admin_set_user_password(Permanent)`, `admin_add_user_to_group` |
| Credential provider | `aws_bedrockagentcore_oauth2_credential_provider` | `create_oauth2_credential_provider` / `update_...` (`identity.py`) |
| IAM roles | `aws_iam_role` + `aws_iam_role_policy` (+ `aws_iam_policy_document` data) + `time_sleep` | `create_role`, `put_role_policy`, `role_exists` waiter, then `call_with_iam_retry` on the consumer (`iam.py`) |
| Lambda | `aws_lambda_function`, `aws_cloudwatch_log_group` | `create_log_group`, `put_retention_policy`, `create_function` / `update_function_code` / `update_function_configuration` (`lambda_tools.py`) |
| Runtimes | `aws_bedrockagentcore_agent_runtime` × 2 + log group | `create_agent_runtime` / `update_agent_runtime`, poll `get_agent_runtime` until `READY` (`runtime_common.py`) |
| Policy engine | `aws_bedrockagentcore_policy_engine` | `create_policy_engine`, poll until `ACTIVE` (`policy.py`) |
| Gateway | `aws_bedrockagentcore_gateway` | `create_gateway` / `update_gateway`, poll until `READY` (`gateway.py`) |
| Targets | `aws_bedrockagentcore_gateway_target` × 2 | `create_gateway_target` / `update_gateway_target`, `synchronize_gateway_targets` |
| Cedar policies | `aws_bedrockagentcore_policy` (`for_each` over `*.cedar`) | `create_policy` / `update_policy` / `delete_policy`, poll until `ACTIVE` |
| Budget | `aws_budgets_budget` (`enable_budget`) | `delete_budget` + `create_budget` (`--budget-email`) |
| Outputs | `local_file`, `local_sensitive_file` | `outputs.py`, `cognito.py` write the JSON files |

Sizes, for a feel: the Terraform root plus six modules is about 1,500 lines of HCL, including module
variables and outputs. The boto3 path is about 2,600 lines of Python, including extensive docstrings,
drift checks and wait loops that Terraform's provider handles for you.

## Topic by topic

| Topic | Terraform | boto3 |
|---|---|---|
| **Dependency ordering** | Implicit from references (`module.gateway.gateway_url` → agent runtime). Hidden orderings still need `depends_on`: policies after both targets, `support` target after `orders`, outputs that wait for uploads | Explicit and visible: the step list in `deploy.py` with a "why here" comment for each |
| **Idempotency** | Built in: re-apply converges | Hand-written get-or-create-or-update in every step. Works, but every new field needs a drift check |
| **Drift** | `terraform plan` shows all managed drift (when the provider reads the attribute back) | Detected only for the fields each step compares. Anything else goes unnoticed |
| **Eventual consistency** | `time_sleep` 15 s after IAM policies, then hope | Polling with status output, and `call_with_iam_retry` with backoff on role errors |
| **Long-running creates** | Provider waits. Progress is a counter ("Still creating... 2m10s") | Custom polling prints each status change (`CREATING → READY`) and failure reasons |
| **Failure recovery** | Partial apply is recorded in state. Fix and re-apply. A failed create may leave a tainted resource that's replaced next time | State saved after each step. Re-run resumes. `CREATE_FAILED` runtimes, gateways, targets and policies are deleted and recreated automatically |
| **Teardown** | `terraform destroy` reverses the graph. `force_destroy` on the bucket. Leftovers created by services (e.g. a log group recreated after delete) are outside state | `destroy.py` deletes by name, continues past failures, sweeps log groups by prefix, and works without the state file |
| **New-feature lag** | AgentCore needs `hashicorp/aws` ≥ 6.71. New API fields arrive when the provider supports them. Some shapes differ from the API (`prefix` is the zip key, the inline tool schema becomes nested `property` blocks) | Available as soon as botocore has it. The request is the API shape (`tools_schema.json` passes through unchanged) |
| **Cross-tool glue** | Shell-out for seed data (`local-exec`), `replace()` for the Cedar templates (`templatefile` doesn't understand `{{...}}`) | Plain Python: subprocess, string replace, JSON |
| **Secrets** | M2M client secret and demo passwords end up in Terraform state | Secret read from Cognito when needed, not stored in `state.json`. Passwords only in `.deploy/users.json` |
| **MCP-target tool refresh** | No resource changes when the MCP server's tools change, so re-sync by hand | `SynchronizeGatewayTargets` when the MCP runtime was updated |
| **Readability for learning** | Short and declarative, but the API calls and their order are hidden | Every API call, order and wait is visible, with docstrings explaining each AgentCore concept |
| **Testability** | `terraform validate`, `plan` | Pure `build_*_request()` functions that unit tests can check offline |
| **CI/CD fit** | Standard (plan on PR, apply on merge, remote state + locking). Teams already know it | Possible, but you own plan/review semantics (there is no "plan"), locking and drift reporting |
| **Team / long-term** | Strong: the standard choice for shared, long-lived infra | Weak beyond a PoC: the hand-written convergence logic grows with every feature |

Gotchas both paths had to handle (the same AgentCore facts expressed twice):

- The Cognito domain must exist before the credential provider reads the discovery document.
- The policy engine comes before the Gateway, and the policies after the targets (`CreatePolicy`
  validates action names).
- The support MCP runtime must be `READY` before the `support` target is created (the Gateway lists
  its tools at creation).
- Runtime log groups are named after the runtime *id*, so they can only be pre-created with a retention
  right after the runtime exists.
- A pinned S3 `versionId` is what turns a rebuilt zip into a runtime or Lambda update.
- Targets are created one at a time, because concurrent changes on one Gateway can conflict.

## Recommendation

**For this PoC: use Terraform to deploy and the boto3 code to learn.**

- Use **Terraform** when you want a reliable deploy/destroy cycle, a reviewable `plan`, a path to
  CI/CD, or a starting point for a shared environment. Add a remote backend (S3 + locking) before
  anyone else uses it.
- Use **boto3** when you're learning what AgentCore actually does (each call and status is visible),
  when you need an API feature the provider doesn't support yet, when Terraform isn't available
  (locked-down workshop laptops), or when you want readable, resumable progress output during a
  live demo.
- Don't use both on one stack. They share names and don't know about each other's state.

| Pick | If |
|---|---|
| Terraform | Shared or long-lived environment; CI/CD; drift detection matters; team already uses Terraform |
| boto3 | Exploring the AgentCore APIs; a feature not in the provider yet; one-off demo; no Terraform available |
| Neither (CDK / CloudFormation) | Your organisation standardises on them. Check AgentCore resource support first |
