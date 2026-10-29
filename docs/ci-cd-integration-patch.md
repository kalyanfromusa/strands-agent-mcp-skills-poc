# CI/CD integration patch (pending)

These edits to **existing** files wire container mode into the main stacks. They are written down
here instead of applied because the existing files were being edited (comment pass) at the same
time. Apply them once that pass is done. The new files they rely on already exist:
`agent/Dockerfile`, `mcp_servers/support_mcp/Dockerfile`, `.github/workflows/deploy.yml`,
`scripts/ci/update_runtime_image.py`, `infra/terraform/ci/` and `docs/10-ci-cd.md`.

Design rules (decided):
- Zip stays the default: `artifact_type = "code"`, so nothing changes for existing users.
- In container mode, Terraform/boto3 set the image **once** (at create or mode switch). After that the
  pipeline owns the image: Terraform `ignore_changes` the container URI, and boto3 keeps the current
  image.
- The orders Lambda stays a zip.

Block names are checked against `.ref/tf_agentcore_schema.txt`
(`agent_runtime_artifact { code_configuration {...} | container_configuration { container_uri (req) } }`).
The `ignore_changes` behaviour was checked with a scratch plan against a hand-made state, using the
provider in `infra/terraform/ci/.terraform`:

| State | Config | Plan |
|---|---|---|
| container `:OLD` | container `:NEW` | **No changes** (the URI is ignored) |
| container `:OLD` | container `:NEW` + another field changed | in-place update, artifact left at `:OLD` |
| code | container | **replace** (provider: `agent_runtime_artifact` *forces replacement*); the new URI is taken from config |
| container | code | **replace** |

So a mode switch recreates the runtime (new id/ARN). Within a mode, everything is an in-place update.

---

## 1. Terraform: `infra/terraform/variables.tf` (append)

```hcl
# Delivery mode for the two AgentCore runtimes (docs/10-ci-cd.md). The orders Lambda is always a zip.
variable "artifact_type" {
  description = "code = zips from scripts/build.py (default); container = images in ECR built by CI."
  type        = string
  default     = "code"

  validation {
    condition     = contains(["code", "container"], var.artifact_type)
    error_message = "artifact_type must be \"code\" or \"container\"."
  }
}

variable "agent_image_uri" {
  description = "Initial agent image (container mode), e.g. <acct>.dkr.ecr.<region>.amazonaws.com/shopassist-agent:<sha>. Later changes come from CI and are ignored."
  type        = string
  default     = ""

  validation {
    condition     = var.artifact_type == "code" || var.agent_image_uri != ""
    error_message = "agent_image_uri is required when artifact_type = \"container\"."
  }
}

variable "support_mcp_image_uri" {
  description = "Initial support MCP image (container mode). Later changes come from CI and are ignored."
  type        = string
  default     = ""

  validation {
    condition     = var.artifact_type == "code" || var.support_mcp_image_uri != ""
    error_message = "support_mcp_image_uri is required when artifact_type = \"container\"."
  }
}
```

`terraform.tfvars.example`: add the three variables, commented out. `docs/04-deploy-terraform.md`:
add three rows to the variables table.

## 2. Terraform: `infra/terraform/main.tf`

**2a. Only require the runtime zips in code mode.** Before:

```hcl
  artifacts = {
    agent         = "${local.repo_root}/dist/agent.zip"
    support_mcp   = "${local.repo_root}/dist/support_mcp.zip"
    orders_lambda = "${local.repo_root}/dist/orders_lambda.zip"
  }
```

After:

```hcl
  artifacts = merge(
    { orders_lambda = "${local.repo_root}/dist/orders_lambda.zip" },
    var.artifact_type == "code" ? {
      agent       = "${local.repo_root}/dist/agent.zip"
      support_mcp = "${local.repo_root}/dist/support_mcp.zip"
    } : {}
  )
```

**2b. `module "mcp_runtime"`.** Before:

```hcl
  zip_key            = module.data.artifact_keys["support_mcp"]
  zip_version_id     = module.data.artifact_version_ids["support_mcp"]
```

After:

```hcl
  zip_key            = try(module.data.artifact_keys["support_mcp"], "")
  zip_version_id     = try(module.data.artifact_version_ids["support_mcp"], "")
  artifact_type      = var.artifact_type
  container_uri      = var.support_mcp_image_uri
```

**2c. `module "agent_runtime"`.** Same change with `"agent"` and `var.agent_image_uri`:

```hcl
  zip_key        = try(module.data.artifact_keys["agent"], "")
  zip_version_id = try(module.data.artifact_version_ids["agent"], "")
  artifact_type  = var.artifact_type
  container_uri  = var.agent_image_uri
```

**2d. Optional: add `artifact_type = var.artifact_type` to `local.deploy_outputs`.** This changes the
`.deploy/outputs.json` contract, so update design.md section 6 first (and boto3 `outputs.py` to
match).

## 3. Terraform: `modules/agent_runtime` and `modules/mcp_runtime`

Make the same edits in both modules. The snippets show `agent_runtime`. For `mcp_runtime`, replace
`app.py` with `server.py` and `-agent` with `-support-mcp`.

**3a. `variables.tf` (append):**

```hcl
variable "artifact_type" {
  description = "code (zip in S3) or container (image in ECR)."
  type        = string
  default     = "code"
}

variable "container_uri" {
  description = "Initial image in container mode; afterwards CI updates it and Terraform ignores it."
  type        = string
  default     = ""
}
```

**3b. `main.tf` locals.** Before:

```hcl
locals {
  runtime_name = "${var.name_prefix}_agent" # runtime names: [a-zA-Z][a-zA-Z0-9_]{0,47}
  runtime_logs = "arn:aws:logs:${var.region}:${var.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"
}
```

After (add one line; this is the repository created by `infra/terraform/ci/ecr.tf`):

```hcl
locals {
  runtime_name = "${var.name_prefix}_agent" # runtime names: [a-zA-Z][a-zA-Z0-9_]{0,47}
  runtime_logs = "arn:aws:logs:${var.region}:${var.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"
  ecr_repo_arn = "arn:aws:ecr:${var.region}:${var.account_id}:repository/${var.name_prefix}-agent" # -support-mcp in mcp_runtime
}
```

**3c. `data "aws_iam_policy_document" "permissions"`: add the ECR pull statements in container mode
only, and keep `ReadCodeZip` in code mode only.** Before:

```hcl
  statement {
    sid       = "ReadCodeZip"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["arn:aws:s3:::${var.bucket}/${var.zip_key}"]
  }
```

After:

```hcl
  # Code mode: read the own zip. Container mode: pull the own image from ECR.
  dynamic "statement" {
    for_each = var.artifact_type == "code" ? [1] : []
    content {
      sid       = "ReadCodeZip"
      actions   = ["s3:GetObject", "s3:GetObjectVersion"]
      resources = ["arn:aws:s3:::${var.bucket}/${var.zip_key}"]
    }
  }
  dynamic "statement" {
    for_each = var.artifact_type == "container" ? [1] : []
    content {
      sid       = "EcrPullImage"
      actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
      resources = [local.ecr_repo_arn]
    }
  }
  dynamic "statement" {
    for_each = var.artifact_type == "container" ? [1] : []
    content {
      sid       = "EcrToken"
      actions   = ["ecr:GetAuthorizationToken"]
      resources = ["*"] # cannot be scoped to a repository
    }
  }
```

**3d. `resource "aws_bedrockagentcore_agent_runtime" "this"`: artifact and lifecycle.** Before:

```hcl
  agent_runtime_artifact {
    code_configuration {
      entry_point = ["app.py"]
      runtime     = "PYTHON_3_12"
      code {
        s3 {
          bucket = var.bucket
          prefix = var.zip_key # the object key of the zip, despite the name
          # A new version id (new zip) is a diff => in-place runtime update.
          version_id = var.zip_version_id
        }
      }
    }
  }
```

After:

```hcl
  # Exactly one of the two blocks is rendered. Switching between them REPLACES the runtime
  # (new id/ARN): the provider treats the artifact type as immutable.
  agent_runtime_artifact {
    dynamic "code_configuration" {
      for_each = var.artifact_type == "code" ? [1] : []
      content {
        entry_point = ["app.py"]
        runtime     = "PYTHON_3_12"
        code {
          s3 {
            bucket = var.bucket
            prefix = var.zip_key # the object key of the zip, despite the name
            # A new version id (new zip) is a diff => in-place runtime update.
            version_id = var.zip_version_id
          }
        }
      }
    }
    dynamic "container_configuration" {
      for_each = var.artifact_type == "container" ? [1] : []
      content {
        container_uri = var.container_uri
      }
    }
  }
```

Then, next to `depends_on = [time_sleep.iam]`, add:

```hcl
  # Container mode: the CI pipeline (scripts/ci/update_runtime_image.py) owns the image after the
  # first apply, so don't roll it back. A path that doesn't exist (code mode) is ignored harmlessly.
  lifecycle {
    ignore_changes = [agent_runtime_artifact[0].container_configuration[0].container_uri]
  }
```

Also update the header comment ("Step by step" 4 and "Key terms": container mode, ECR) and the
Step 4 comment above the resource.

## 4. boto3: `infra/boto3/common.py`

**4a. Names (add to `class Names`):**

```python
    # ECR repositories for container mode (created by infra/terraform/ci, not by deploy.py).
    @property
    def agent_ecr_repo(self) -> str:
        return f"{self.prefix}-agent"

    @property
    def mcp_ecr_repo(self) -> str:
        return f"{self.prefix}-support-mcp"

    def ecr_repo_arn(self, repo: str) -> str:
        return f"arn:aws:ecr:{self.region}:{self.account_id}:repository/{repo}"
```

**4b. Context (add fields after `model_id`):**

```python
    artifact_type: str = "code"          # "code" | "container"
    agent_image: str | None = None       # container mode: initial image URIs
    support_mcp_image: str | None = None
```

**4c. `make_context`:** add the parameters `artifact_type="code", agent_image=None,
support_mcp_image=None` and pass them to `Context(...)`.

## 5. boto3: `infra/boto3/steps/runtime_common.py`

Add a builder next to `build_code_artifact`:

```python
def build_container_artifact(image_uri: str) -> dict:
    """The `agentRuntimeArtifact` field for container mode: an arm64 image in ECR (tag or digest)."""
    return {"containerConfiguration": {"containerUri": image_uri}}
```

In `ensure_runtime`, keep the image the pipeline deployed (the boto3 equivalent of
`ignore_changes`). Insert after `current = _get(ac, runtime_id)` in Step 3:

```python
    # Container mode: after creation, CI owns the image (scripts/ci/update_runtime_image.py). Keep
    # whatever image is running so a re-run of deploy.py doesn't roll it back.
    cur_art = current.get("agentRuntimeArtifact") or {}
    if "containerConfiguration" in cur_art and "containerConfiguration" in create_request["agentRuntimeArtifact"]:
        create_request = {**create_request, "agentRuntimeArtifact": cur_art}
```

A code↔container switch goes through `UpdateAgentRuntime` in place here (the API allows it), unlike
Terraform, which replaces the runtime. Mention this difference in docs/06.

## 6. boto3: `steps/agent_runtime.py` and `steps/mcp_runtime.py`

In `build_create_request`, before:

```python
        "agentRuntimeArtifact": rc.build_code_artifact(s3["bucket"], art["key"], art["version_id"], ENTRY_POINT),
```

After (agent; for mcp use `ctx.support_mcp_image`):

```python
        "agentRuntimeArtifact": (rc.build_container_artifact(ctx.agent_image) if ctx.artifact_type == "container"
                                 else rc.build_code_artifact(s3["bucket"], art["key"], art["version_id"], ENTRY_POINT)),
```

Move `art = s3["artifacts"]["agent"]` inside the code branch (or use `.get`), because container mode
doesn't upload that zip (section 8).

## 7. boto3: `steps/iam.py`

Add a helper and use it in `mcp_runtime_permissions` / `agent_runtime_permissions`:

```python
def _artifact_access(n: Names, artifact_type: str, zip_name: str, repo: str) -> list[dict]:
    """Code mode: read the own zip. Container mode: pull the own image (token call can't be scoped)."""
    if artifact_type == "container":
        return [{"Sid": "EcrPullImage", "Effect": "Allow",
                 "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], "Resource": n.ecr_repo_arn(repo)},
                {"Sid": "EcrToken", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"}]
    return [_artifact_read(n, zip_name)]
```

- `mcp_runtime_permissions(n)` → `mcp_runtime_permissions(n, artifact_type)`: replace
  `_artifact_read(n, "support_mcp.zip"),` with `*_artifact_access(n, artifact_type, "support_mcp.zip", n.mcp_ecr_repo),`.
- `agent_runtime_permissions(n)` → `agent_runtime_permissions(n, artifact_type)`: replace
  `_artifact_read(n, "agent.zip"),` with `*_artifact_access(n, artifact_type, "agent.zip", n.agent_ecr_repo),`.
- In `role_documents(ctx)`, pass `ctx.artifact_type` to both.

## 8. boto3: `steps/s3.py` and `deploy.py`

`s3.deploy`, before: `for name, path in ARTIFACTS.items():`. After:

```python
    wanted = ARTIFACTS if ctx.artifact_type == "code" else {"orders_lambda": ARTIFACTS["orders_lambda"]}
    for name, path in wanted.items():
```

`deploy.py parse_args`, add:

```python
    p.add_argument("--artifact-type", choices=["code", "container"], default="code",
                   help="code: zips from scripts/build.py (default); container: images in ECR (docs/10-ci-cd.md)")
    p.add_argument("--agent-image", help="container mode: agent image URI (<acct>.dkr.ecr.<region>.amazonaws.com/shopassist-agent:<sha>)")
    p.add_argument("--support-mcp-image", help="container mode: support MCP image URI")
```

After `args = p.parse_args(argv)`:

```python
    if args.artifact_type == "container" and not (args.agent_image and args.support_mcp_image):
        p.error("--artifact-type container needs --agent-image and --support-mcp-image")
```

`check_artifacts()` → `check_artifacts(args)`: in container mode, check only `ARTIFACTS["orders_lambda"]`.
`main`: `make_context(args.region, args.prefix, args.model_id, args.artifact_type, args.agent_image, args.support_mcp_image)`.
Update the module docstring's usage block. `docs/05-deploy-boto3.md`: document the three flags.

`destroy.py` needs no change: the ECR repositories belong to `infra/terraform/ci` and are destroyed
there.

## 9. Tests (`tests/test_boto3_shapes.py`, append)

```python
def test_container_artifact_validates():
    req = {"agentRuntimeName": "shopassist_agent", "roleArn": f"arn:aws:iam::{ACCOUNT}:role/r",
           "agentRuntimeArtifact": M["runtime_common"].build_container_artifact(
               f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/shopassist-agent:abc"),
           "networkConfiguration": {"networkMode": "PUBLIC"}}
    report = ParamValidator().validate(req, client("bedrock-agentcore-control").meta.service_model
                                       .operation_model("CreateAgentRuntime").input_shape)
    assert not report.has_errors(), report.generate_report()
```

Also cover the IAM documents in container mode (statement `EcrPullImage` on the right repository,
no `ReadOwnArtifact`), and `ensure_runtime` keeping the current image (use a fake client like the one
in `tests/test_ci_update_runtime.py`).

## 10. Docs and spec

- `.kiro/specs/strands-poc/design.md`. **Edit this first** (contracts):
  section 1 naming: add the ECR repos `shopassist-agent`, `shopassist-support-mcp` and the CI role
  `shopassist-github-ci`. Section 5 packaging: add "container mode: `agent/Dockerfile`,
  `mcp_servers/support_mcp/Dockerfile`, build context = repo root, linux/arm64". Add a new
  section 9, "CI/CD", with the repo variables and the rule "pipeline swaps images only; Terraform
  `ignore_changes` container_uri".
- `.kiro/steering/decisions.md`: add `2026-10-29 Optional container delivery (GitHub Actions → ECR →
  UpdateAgentRuntime); zip stays the default; Terraform ignores the image in container mode.` The
  current entry "Direct-code (zip) Runtime deployment, so no ECR or Docker is needed" should then
  read "by default".
- `.kiro/steering/structure.md`: add `.github/workflows`, `scripts/ci`, `infra/terraform/ci`.
- `.kiro/specs/strands-poc/tasks.md`: add the task(s) for this work.
- `README.md`: in the Documentation list add `10. [CI/CD with container images](docs/10-ci-cd.md)`.
  In the layout table add `.github/workflows/`, `scripts/ci/` and `infra/terraform/ci/`.
- `docs/03-architecture.md` (Packaging section, "Direct code deployment, so no Docker or ECR is
  needed"): add "by default; see 10-ci-cd.md for container mode".
- `docs/06-terraform-vs-boto3.md`: add a row "Runtime artifact switch": Terraform replaces the
  runtime (new id), boto3 updates it in place. Add another row: both keep the CI-deployed image
  (`ignore_changes` vs keeping the current artifact).
- `docs/07-cost.md`: ECR ≈ $0.25/month in container mode.
- `docs/09-troubleshooting.md`: add entries for `AccessDenied` on `ecr:BatchGetImage` (runtime role is
  missing the section 3c/7 statements), "Not authorized to perform sts:AssumeRoleWithWebIdentity"
  (wrong branch or a GitHub environment changed `sub`), and an "exec format error" runtime
  failure (image not built for arm64).
- Root `.gitignore`: optional `infra/terraform/ci/.terraform/` and `infra/terraform/ci/*.tfstate*`.
  `infra/terraform/ci/.gitignore` already covers them locally.

## Open questions

1. **Pinned dependencies in images.** Both Dockerfiles install `requirements.txt` (`>=` ranges), the
   same as `scripts/build.py`. For reproducible images, generate a lock per image with
   `uv export --frozen --no-dev --no-hashes` (or a `uv pip compile` of each `requirements.txt`) and
   install that instead.
2. **Observability.** Neither direct-code nor these images wrap the process in
   `opentelemetry-instrument` (AWS's starter-toolkit images do). Add ADOT if you want GenAI traces.
3. **Extra IAM for UpdateAgentRuntime.** This is untested: AWS may require workload-identity or ECR
   read actions on the *caller* during an update. Add whatever the first `AccessDenied` names.
4. **Gateway id format.** The CI policy defaults to `gateway/<prefix>-gateway-*`. If the Gateway's id
   doesn't start with its name, set `gateway_id` in `infra/terraform/ci/terraform.tfvars`.
