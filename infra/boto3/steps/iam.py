"""IAM execution roles: what each component is allowed to do *as itself*.

What this file does
    Deploy step 5. Creates four IAM roles (permission sets), one for each component that runs code or
    calls other services, and writes the permission document for each.

Where it fits
    It needs the credential provider (step 3, for its secret's ARN) and the policy engine (step 4, for its
    ARN), because the Gateway role is limited to exactly those. The Lambda (step 7), the two runtimes
    (steps 8 and 11) and the Gateway (step 9) are each created with one of these roles.

| Role | Assumed by | Can |
|---|---|---|
| `<prefix>-orders-tools-role` | Lambda | read/write the orders and returns tables, write its logs |
| `<prefix>-support-mcp-runtime-role` | AgentCore Runtime (support MCP) | CRUD on the tickets table, logs/traces |
| `<prefix>-gateway-role` | AgentCore Gateway | invoke the Lambda, fetch OAuth tokens from AgentCore Identity (+ read the provider's secret) for the support target, evaluate Cedar policies |
| `<prefix>-agent-runtime-role` | AgentCore Runtime (agent) | call Bedrock models, read skills from S3, logs/traces/metrics |

The agent role has no OAuth2 token-vault permissions: the deployed agent forwards the *user's* token to
the Gateway (design.md section 8) instead of minting its own.

AgentCore trust policies are scoped with `aws:SourceAccount` / `aws:SourceArn` (confused-deputy guard).
Role names and inline policy names (`orders-tools`, `support-mcp-runtime`, `gateway`, `agent-runtime`) are
the same as in Terraform. Each role gets that one inline policy, overwritten on every deploy, so a re-run
always converges.

Step by step (for each of the four roles)
    1. If the role exists, refresh its trust policy and tags; otherwise create it and wait until it exists.
    2. Write (overwrite) its one permission policy.
    3. Save the role name and ARN in state.json.

Key terms
    IAM role: a set of AWS permissions that a service is allowed to use; it has two documents:
    Trust policy: WHO may use the role (e.g. "the Lambda service").
    Permissions policy: WHAT the role may do (e.g. "read the orders table").
    Assume a role: a service temporarily takes on the role's permissions.
    Confused deputy: a trick where another AWS customer gets a service to act with your role; the
    SourceAccount/SourceArn conditions block that.
    CRUD: create, read, update, delete.
    X-Ray / traces: AWS's request-tracing service, used to see where time is spent.
"""

from __future__ import annotations

from common import Context, Names, OK, info, is_not_found, policy_json, tag_list

# The four roles, also the order they are created and deleted in.
ROLE_KINDS = ("lambda", "mcp-runtime", "gateway", "agent-runtime")


# ------------------------------------------------------------------ policy documents (pure)


def trust_lambda() -> str:
    """Trust policy: only the AWS Lambda service may use this role."""
    return policy_json([{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                         "Action": "sts:AssumeRole"}])


def trust_agentcore(n: Names) -> str:
    """Trust policy: only AgentCore, and only when acting for a resource in our own account."""
    return policy_json([{
        "Effect": "Allow",
        "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
        "Action": "sts:AssumeRole",
        "Condition": {
            "StringEquals": {"aws:SourceAccount": n.account_id},
            "ArnLike": {"aws:SourceArn": n.agentcore_arn("*")},
        },
    }])


def _runtime_basics(n: Names, runtime_name: str) -> list[dict]:
    """Logs, traces, metrics and workload identity: what every AgentCore Runtime execution role needs."""
    group = f"arn:aws:logs:{n.region}:{n.account_id}:log-group:/aws/bedrock-agentcore/runtimes/{runtime_name}-*"
    return [
        # Write log lines, only into this runtime's own log groups.
        {"Sid": "Logs", "Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:DescribeLogStreams", "logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": [group, f"{group}:log-stream:*"]},
        {"Sid": "DescribeLogGroups", "Effect": "Allow", "Action": "logs:DescribeLogGroups",
         "Resource": f"arn:aws:logs:{n.region}:{n.account_id}:log-group:*"},
        # Send request traces to X-Ray.
        {"Sid": "XRay", "Effect": "Allow",
         "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules",
                    "xray:GetSamplingTargets"],
         "Resource": "*"},
        # Publish metrics, but only under the "bedrock-agentcore" namespace.
        {"Sid": "Metrics", "Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
         "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}},
        # With a JWT inbound authorizer the runtime exchanges the caller's JWT for a workload access token
        # (passed to the code in the WorkloadAccessToken header) using this role. Same as Terraform.
        {"Sid": "WorkloadIdentity", "Effect": "Allow",
         "Action": ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
         "Resource": [n.agentcore_arn("workload-identity-directory/default"),
                      n.agentcore_arn(f"workload-identity-directory/default/workload-identity/{runtime_name}-*")]},
    ]


def _artifact_read(n: Names, zip_name: str) -> dict:
    """Permission to read one code zip from the artifacts bucket (and no other file)."""
    # Direct code deployment reads the zip from S3. Granting the role read access to its own zip is
    # harmless and avoids depending on whether the service uses the caller or the role for the fetch.
    return {"Sid": "ReadOwnArtifact", "Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"],
            "Resource": f"arn:aws:s3:::{n.bucket}/artifacts/{zip_name}"}


def lambda_permissions(n: Names) -> str:
    """Orders Lambda: read/write the orders and returns tables (and their indexes), and write its logs."""
    orders, returns = n.table_arn(n.orders_table), n.table_arn(n.returns_table)
    return policy_json([
        {"Sid": "OrdersAndReturns", "Effect": "Allow",
         "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query",
                    "dynamodb:Scan"],
         "Resource": [orders, f"{orders}/index/*", returns, f"{returns}/index/*"]},
        {"Sid": "Logs", "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{n.region}:{n.account_id}:log-group:/aws/lambda/{n.lambda_function}:*"},
    ])


def mcp_runtime_permissions(n: Names) -> str:
    """Support MCP server: full access to the tickets table only, its own zip, and the runtime basics."""
    tickets = n.table_arn(n.tickets_table)
    return policy_json([
        {"Sid": "Tickets", "Effect": "Allow",
         "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
                    "dynamodb:Query", "dynamodb:Scan"],
         "Resource": [tickets, f"{tickets}/index/*"]},
        _artifact_read(n, "support_mcp.zip"),
        *_runtime_basics(n, n.mcp_runtime),
    ])


def gateway_permissions(n: Names, provider_arn: str, provider_secret_arn: str, engine_arn: str | None = None) -> str:
    """Gateway: call the orders Lambda, get OAuth tokens for the support target, and ask the Cedar rulebook."""
    # The gateway does not exist yet when its role is created (the role is an input of CreateGateway),
    # so its ARN is a name pattern: gateway IDs are "<name>-<10 chars>". The policy engine is created
    # before this step, so its exact ARN is normally known; the pattern is only a fallback.
    gateway_arn = n.agentcore_arn(f"gateway/{n.gateway}-*")
    engine_arn = engine_arn or n.agentcore_arn(f"policy-engine/{n.policy_engine}-*")
    return policy_json([
        # Target `orders`: outbound credential = this role.
        {"Sid": "InvokeOrdersLambda", "Effect": "Allow", "Action": "lambda:InvokeFunction",
         "Resource": [n.lambda_arn, f"{n.lambda_arn}:*"]},
        # Target `support`: outbound OAuth. The Gateway gets a workload access token for itself, exchanges
        # it for a Cognito client_credentials token via the credential provider, which reads its secret.
        {"Sid": "AgentCoreIdentityTokens", "Effect": "Allow",
         "Action": ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetResourceOauth2Token"],
         "Resource": [n.agentcore_arn("workload-identity-directory/default"),
                      n.agentcore_arn("workload-identity-directory/default/workload-identity/*"),
                      n.agentcore_arn("token-vault/default"),
                      n.agentcore_arn("token-vault/default/*"),
                      provider_arn]},
        {"Sid": "ReadProviderSecret", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
         "Resource": provider_secret_arn},
        # AgentCore Policy (Cedar), per docs/policy-permissions: AuthorizeAction (tools/call),
        # PartiallyAuthorizeActions (filtered tools/list) and GetPolicyEngine.
        {"Sid": "PolicyEngineConfiguration", "Effect": "Allow", "Action": "bedrock-agentcore:GetPolicyEngine",
         "Resource": engine_arn},
        {"Sid": "PolicyEngineAuthorization", "Effect": "Allow",
         "Action": ["bedrock-agentcore:AuthorizeAction", "bedrock-agentcore:PartiallyAuthorizeActions"],
         "Resource": [engine_arn, gateway_arn]},
    ])


def agent_runtime_permissions(n: Names) -> str:
    """Agent: call the Bedrock AI model, read the skills folder in S3, its own zip, and the runtime basics."""
    return policy_json([
        {"Sid": "BedrockInvoke", "Effect": "Allow",
         "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
         # Cross-region inference profiles (us.*) route to foundation models in several regions.
         "Resource": ["arn:aws:bedrock:*::foundation-model/*",
                      f"arn:aws:bedrock:{n.region}:{n.account_id}:inference-profile/*"]},
        {"Sid": "ReadSkills", "Effect": "Allow", "Action": "s3:GetObject",
         "Resource": f"arn:aws:s3:::{n.bucket}/skills/*"},
        # Listing the bucket is limited to the skills/ folder, so the agent can't browse the code zips.
        {"Sid": "ListSkills", "Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{n.bucket}",
         "Condition": {"StringLike": {"s3:prefix": ["skills/*", "skills/"]}}},
        _artifact_read(n, "agent.zip"),
        *_runtime_basics(n, n.agent_runtime),
    ])


def role_documents(ctx: Context) -> dict[str, tuple[str, str, str]]:
    """kind -> (description, trust policy, permissions policy)."""
    n = ctx.names
    ident = ctx.state.section("identity")
    return {
        "lambda": ("ShopAssist orders tools Lambda", trust_lambda(), lambda_permissions(n)),
        "mcp-runtime": ("ShopAssist support MCP runtime", trust_agentcore(n), mcp_runtime_permissions(n)),
        "gateway": ("ShopAssist AgentCore Gateway", trust_agentcore(n),
                    gateway_permissions(n, ident["arn"], ident["secret_arn"],
                                        ctx.state.section("policy").get("engine_arn"))),
        "agent-runtime": ("ShopAssist agent runtime", trust_agentcore(n), agent_runtime_permissions(n)),
    }


# ------------------------------------------------------------------ request builders (pure)


def build_create_role_request(role_name: str, description: str, trust: str, tags: dict[str, str]) -> dict:
    """CreateRole request. MaxSessionDuration 3600: each use of the role lasts at most one hour."""
    return {"RoleName": role_name, "Description": description, "AssumeRolePolicyDocument": trust,
            "MaxSessionDuration": 3600, "Tags": tag_list(tags)}


def build_put_role_policy_request(role_name: str, policy_name: str, document: str) -> dict:
    """PutRolePolicy request: store the permissions document inside the role (create or overwrite)."""
    return {"RoleName": role_name, "PolicyName": policy_name, "PolicyDocument": document}


# ------------------------------------------------------------------ deploy / destroy


def deploy(ctx: Context) -> None:
    """Create or refresh the four roles and their permissions."""
    iam = ctx.client("iam")
    n = ctx.names
    roles = {}
    for kind, (description, trust, permissions) in role_documents(ctx).items():
        name = n.role(kind)
        # Step 1: AWS API GetRole. If the role exists, re-apply its trust policy (UpdateAssumeRolePolicy)
        # and tags (TagRole). If it is missing, CreateRole, then wait until IAM reports it exists.
        try:
            arn = iam.get_role(RoleName=name)["Role"]["Arn"]
            iam.update_assume_role_policy(RoleName=name, PolicyDocument=trust)
            iam.tag_role(RoleName=name, Tags=tag_list(n.tags))
            info(f"role {name} exists, trust policy refreshed")
        except Exception as err:  # noqa: BLE001
            if not is_not_found(err):
                raise
            arn = iam.create_role(**build_create_role_request(name, description, trust, n.tags))["Role"]["Arn"]
            iam.get_waiter("role_exists").wait(RoleName=name)
            info(f"{OK} created role {name}")
        # Step 2: AWS API PutRolePolicy always overwrites, so the permissions match the code on every run.
        iam.put_role_policy(**build_put_role_policy_request(name, n.role_basename(kind), permissions))
        # Step 3: save after each role, so a crash halfway still records the roles already made.
        roles[kind] = {"name": name, "arn": arn}
        ctx.state.put("iam", roles=roles)
    # "Exists" in IAM is not the same as "usable by every service" yet; see call_with_iam_retry in common.py.
    info("note: new roles can take ~10s to be assumable; later steps retry on IAM propagation errors")


def destroy(ctx: Context) -> None:
    """Delete the four roles. AWS requires a role's policies to be removed before the role itself."""
    iam = ctx.client("iam")
    for kind in ROLE_KINDS:
        name = ctx.names.role(kind)
        try:
            # Step 1: delete inline policies (ListRolePolicies + DeleteRolePolicy).
            for policy in iam.list_role_policies(RoleName=name)["PolicyNames"]:
                iam.delete_role_policy(RoleName=name, PolicyName=policy)
            # Step 2: detach any shared managed policies (none are attached by this script, but be safe).
            for attached in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
                iam.detach_role_policy(RoleName=name, PolicyArn=attached["PolicyArn"])
            # Step 3: delete the role itself (DeleteRole).
            iam.delete_role(RoleName=name)
            info(f"{OK} deleted role {name}")
        except Exception as err:  # noqa: BLE001
            if not is_not_found(err):
                raise
            info(f"role {name} already gone")
    ctx.state.drop("iam")
