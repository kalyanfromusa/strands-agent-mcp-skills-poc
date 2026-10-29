"""infra/boto3: every request the deploy sends, validated offline against the installed botocore models.

The step modules build their requests in pure `build_*_request()` functions. Here each one is fed a fake
deployment state and checked with `botocore.validate.ParamValidator` (unknown keys, missing required
members, types) plus an enum check (botocore doesn't validate enums client-side). The status names the
code waits for, the waiters and paginators it uses, and the design.md contracts are checked as well.
No AWS calls are made: creating a client needs a region but no credentials.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from urllib.parse import unquote

import boto3
import pytest
from botocore.validate import ParamValidator

ROOT = Path(__file__).resolve().parents[1]
BOTO3_DIR = ROOT / "infra" / "boto3"

REGION, ACCOUNT, PREFIX = "us-east-1", "123456789012", "shopassist"


def _load_boto3_modules() -> dict:
    """Import infra/boto3's `common` and `steps.*` without clobbering mcp_servers' `common` package.

    pytest puts `mcp_servers` on sys.path (its `common` package holds the store), and the deploy scripts
    also have a top-level `common` module. Swap sys.modules around the import, then restore it.
    """
    saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "common" or k.startswith("common.")}
    saved_path = list(sys.path)  # deploy.py/destroy.py also insert their own dir at import time
    sys.path.insert(0, str(BOTO3_DIR))
    try:
        mods = {"common": importlib.import_module("common")}
        for name in ("s3", "cognito", "identity", "policy", "iam", "dynamodb", "lambda_tools", "runtime_common",
                     "mcp_runtime", "gateway", "agent_runtime", "budget", "outputs"):
            mods[name] = importlib.import_module(f"steps.{name}")
        mods["deploy"] = importlib.import_module("deploy")
        mods["destroy"] = importlib.import_module("destroy")
        return mods
    finally:
        sys.path[:] = saved_path
        for k in [k for k in sys.modules if k == "common" or k.startswith("common.")]:
            del sys.modules[k]
        sys.modules.update(saved)


M = _load_boto3_modules()
common = M["common"]

_clients: dict = {}


def client(service: str):
    if service not in _clients:
        _clients[service] = boto3.client(service, region_name=REGION, aws_access_key_id="x",
                                         aws_secret_access_key="x")
    return _clients[service]


def _enum_errors(params, shape, path="") -> list[str]:
    errors = []
    if shape.type_name == "structure" and isinstance(params, dict):
        for k, v in params.items():
            if k in shape.members:
                errors += _enum_errors(v, shape.members[k], f"{path}.{k}")
    elif shape.type_name == "list" and isinstance(params, list):
        for i, v in enumerate(params):
            errors += _enum_errors(v, shape.member, f"{path}[{i}]")
    elif shape.type_name == "map" and isinstance(params, dict):
        for k, v in params.items():
            errors += _enum_errors(v, shape.value, f"{path}[{k}]")
    elif shape.type_name == "string" and getattr(shape, "enum", None) and params not in shape.enum:
        errors.append(f"{path}: {params!r} not in {shape.enum}")
    return errors


def validate(service: str, operation: str, params: dict) -> None:
    """Fail with botocore's own report if `params` isn't a valid `operation` request."""
    c = client(service)
    op_name = c.meta.method_to_api_mapping[operation]
    shape = c.meta.service_model.operation_model(op_name).input_shape
    report = ParamValidator().validate(params, shape)
    assert not report.has_errors(), f"{service}.{operation}:\n{report.generate_report()}"
    enum_errors = _enum_errors(params, shape)
    assert not enum_errors, f"{service}.{operation}: " + "; ".join(enum_errors)


def enum(service: str, shape_name: str) -> set[str]:
    return set(client(service).meta.service_model.shape_for(shape_name).enum)


# ------------------------------------------------------------------ fake deployment context

LAMBDA_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{PREFIX}-orders-tools"
MCP_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{PREFIX}_support_mcp-AbCdEf1234"
AGENT_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{PREFIX}_agent-AbCdEf1234"
GATEWAY_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/{PREFIX}-gateway-abcdefghij"
ENGINE_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:policy-engine/{PREFIX}_policy_engine-abcdefghij"
PROVIDER_ARN = (f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:token-vault/default/oauth2credentialprovider/"
                f"{PREFIX}-cognito")
SECRET_ARN = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:bedrock-agentcore-identity!default/oauth2/x-AbCdEf"
POOL_ID = "us-east-1_AbCdEfGhI"
ENGINE_ID = f"{PREFIX}_policy_engine-abcdefghij"
GATEWAY_ID = f"{PREFIX}-gateway-abcdefghij"
DISCOVERY = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_ID}/.well-known/openid-configuration"


@pytest.fixture
def ctx(tmp_path):
    state = common.State(tmp_path / "state.json")
    state.data.update({"region": REGION, "prefix": PREFIX, "account_id": ACCOUNT})
    bucket = f"{PREFIX}-artifacts-{ACCOUNT}-{REGION}"
    state.put("s3", bucket=bucket, skills_s3_uri=f"s3://{bucket}/skills/", artifacts={
        "agent": {"key": "artifacts/agent.zip", "version_id": "v-agent"},
        "support_mcp": {"key": "artifacts/support_mcp.zip", "version_id": "v-mcp"},
        "orders_lambda": {"key": "artifacts/orders_lambda.zip", "version_id": "v-lambda"},
    })
    state.put("cognito", user_pool_id=POOL_ID, discovery_url=DISCOVERY, domain=f"{PREFIX}-{ACCOUNT}",
              token_url=f"https://{PREFIX}-{ACCOUNT}.auth.{REGION}.amazoncognito.com/oauth2/token",
              m2m_client_id="m2mclientid000000000000000", users_client_id="usersclientid0000000000000")
    state.put("identity", name=f"{PREFIX}-cognito", arn=PROVIDER_ARN, secret_arn=SECRET_ARN)
    state.put("policy", engine_id=f"{PREFIX}_policy_engine-abcdefghij", engine_arn=ENGINE_ARN)
    state.put("iam", roles={k: {"name": f"r-{k}", "arn": f"arn:aws:iam::{ACCOUNT}:role/r-{k}"}
                            for k in M["iam"].ROLE_KINDS})
    state.put("lambda", name=f"{PREFIX}-orders-tools", arn=LAMBDA_ARN)
    state.put("mcp_runtime", id=f"{PREFIX}_support_mcp-AbCdEf1234", arn=MCP_ARN, version="1")
    state.put("gateway", id=f"{PREFIX}-gateway-abcdefghij", arn=GATEWAY_ARN, name=f"{PREFIX}-gateway",
              url=f"https://{PREFIX}-gateway-abcdefghij.gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp")
    state.put("agent_runtime", id=f"{PREFIX}_agent-AbCdEf1234", arn=AGENT_ARN, version="1")
    return common.Context(region=REGION, prefix=PREFIX, account_id=ACCOUNT, session=None, state=state)


# ------------------------------------------------------------------ S3, Cognito, DynamoDB, IAM, Lambda, Logs


def test_s3_requests(ctx):
    s3, n = M["s3"], ctx.names
    validate("s3", "create_bucket", s3.build_create_bucket_request(n.bucket, REGION))
    eu = s3.build_create_bucket_request(n.bucket, "eu-west-1")
    validate("s3", "create_bucket", eu)
    assert "CreateBucketConfiguration" not in s3.build_create_bucket_request(n.bucket, "us-east-1")
    validate("s3", "put_bucket_versioning", s3.build_versioning_request(n.bucket))
    validate("s3", "put_public_access_block", s3.build_public_access_block_request(n.bucket))
    validate("s3", "put_bucket_tagging", s3.build_tagging_request(n.bucket, n.tags))


def test_cognito_requests(ctx):
    cg, n = M["cognito"], ctx.names
    validate("cognito-idp", "create_user_pool", cg.build_create_user_pool_request(n.user_pool, n.tags))
    validate("cognito-idp", "create_user_pool_domain", cg.build_create_domain_request(n.cognito_domain, POOL_ID))
    rs = cg.build_resource_server_request(POOL_ID, n.resource_server_id)
    validate("cognito-idp", "create_resource_server", rs)
    validate("cognito-idp", "update_resource_server", rs)
    m2m = cg.build_m2m_client_request(POOL_ID, n.m2m_client, n.scope)
    users = cg.build_users_client_request(POOL_ID, n.users_client)
    for req in (m2m, users):
        validate("cognito-idp", "create_user_pool_client", req)
        validate("cognito-idp", "update_user_pool_client", cg.as_update_client_request(req, "clientid"))
    assert m2m["GenerateSecret"] and m2m["AllowedOAuthFlows"] == ["client_credentials"]
    assert m2m["AllowedOAuthScopes"] == ["shopassist/invoke"]
    assert users["GenerateSecret"] is False
    assert set(users["ExplicitAuthFlows"]) == {"ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"}
    for group, desc in cg.GROUPS.items():
        validate("cognito-idp", "create_group", cg.build_create_group_request(POOL_ID, group, desc))
    validate("cognito-idp", "admin_create_user", cg.build_admin_create_user_request(POOL_ID, "alice"))
    validate("cognito-idp", "admin_set_user_password", cg.build_set_password_request(POOL_ID, "alice", "x" * 20))
    validate("cognito-idp", "admin_add_user_to_group",
             {"UserPoolId": POOL_ID, "Username": "bob", "GroupName": "shop-viewer"})


def test_demo_users_and_passwords():
    cg = M["cognito"]
    assert set(cg.GROUPS) == {"shop-admin", "shop-viewer"}
    assert cg.DEMO_USERS == {"alice": "shop-admin", "bob": "shop-viewer", "carol": ""}
    policy = cg.PASSWORD_POLICY
    for _ in range(50):
        pw = cg.generate_password()
        assert len(pw) >= policy["MinimumLength"]
        assert any(c.isupper() for c in pw) and any(c.islower() for c in pw)
        assert any(c.isdigit() for c in pw) and any(not c.isalnum() for c in pw)


def test_dynamodb_requests(ctx):
    ddb = M["dynamodb"]
    specs = ddb.table_specs(PREFIX)
    assert [s["name"] for s in specs] == ["shopassist-orders", "shopassist-returns", "shopassist-tickets"]
    for s in specs:
        req = ddb.build_create_table_request(s["name"], s["pk"], s["gsi"], ctx.names.tags)
        validate("dynamodb", "create_table", req)
        assert req["BillingMode"] == "PAY_PER_REQUEST"
    orders = ddb.build_create_table_request(specs[0]["name"], "order_id", specs[0]["gsi"], ctx.names.tags)
    assert orders["GlobalSecondaryIndexes"][0]["IndexName"] == "customer_id-index"


def test_iam_requests(ctx):
    iam, n = M["iam"], ctx.names
    docs = iam.role_documents(ctx)
    assert set(docs) == set(iam.ROLE_KINDS)
    for kind, (description, trust, permissions) in docs.items():
        validate("iam", "create_role", iam.build_create_role_request(n.role(kind), description, trust, n.tags))
        validate("iam", "put_role_policy",
                 iam.build_put_role_policy_request(n.role(kind), n.role_basename(kind), permissions))
        for doc in (trust, permissions):
            assert json.loads(doc)["Version"] == "2012-10-17"
    # Same role names as Terraform.
    assert {k: n.role(k) for k in iam.ROLE_KINDS} == {
        "lambda": "shopassist-orders-tools-role", "mcp-runtime": "shopassist-support-mcp-runtime-role",
        "gateway": "shopassist-gateway-role", "agent-runtime": "shopassist-agent-runtime-role"}


def test_gateway_role_has_policy_evaluation_permissions(ctx):
    """devguide/policy-permissions.html: GetPolicyEngine, AuthorizeAction, PartiallyAuthorizeActions."""
    _, _, permissions = M["iam"].role_documents(ctx)["gateway"]
    statements = {s["Sid"]: s for s in json.loads(permissions)["Statement"]}
    assert statements["PolicyEngineConfiguration"]["Action"] == "bedrock-agentcore:GetPolicyEngine"
    assert statements["PolicyEngineConfiguration"]["Resource"] == ENGINE_ARN
    auth = statements["PolicyEngineAuthorization"]
    assert set(auth["Action"]) == {"bedrock-agentcore:AuthorizeAction", "bedrock-agentcore:PartiallyAuthorizeActions"}
    assert ENGINE_ARN in auth["Resource"]
    assert any(":gateway/" in r for r in auth["Resource"])
    assert statements["InvokeOrdersLambda"]["Resource"][0] == LAMBDA_ARN


def test_lambda_and_logs_requests(ctx):
    lt, n = M["lambda_tools"], ctx.names
    env = lt.build_environment(ctx)
    assert env == {"STORE": "dynamodb", "ORDERS_TABLE": "shopassist-orders", "RETURNS_TABLE": "shopassist-returns"}
    req = lt.build_create_function_request(n.lambda_function, f"arn:aws:iam::{ACCOUNT}:role/r", n.bucket, "artifacts/orders_lambda.zip", "v1", env, n.tags)
    validate("lambda", "create_function", req)
    assert req["Architectures"] == ["arm64"] and req["Runtime"] == "python3.12"
    assert req["Handler"] == "handler.lambda_handler"
    validate("lambda", "update_function_configuration",
             lt.build_update_configuration_request(n.lambda_function, f"arn:aws:iam::{ACCOUNT}:role/r", env))
    validate("logs", "create_log_group", lt.build_create_log_group_request("/aws/lambda/x", n.tags))
    validate("logs", "put_retention_policy", lt.build_retention_request("/aws/lambda/x"))
    assert lt.build_retention_request("x")["retentionInDays"] == 7


# ------------------------------------------------------------------ AgentCore Identity and Runtimes


def test_identity_requests(ctx):
    idn, n = M["identity"], ctx.names
    validate("bedrock-agentcore-control", "create_oauth2_credential_provider",
             idn.build_create_provider_request(n.oauth_provider, DISCOVERY, "cid", "secret", n.tags))
    validate("bedrock-agentcore-control", "update_oauth2_credential_provider",
             idn.build_update_provider_request(n.oauth_provider, DISCOVERY, "cid", "secret"))


def test_mcp_runtime_request(ctx):
    rc, req = M["runtime_common"], M["mcp_runtime"].build_create_request(ctx)
    validate("bedrock-agentcore-control", "create_agent_runtime", req)
    validate("bedrock-agentcore-control", "update_agent_runtime", rc.as_update_request(req, "shopassist_agent-AbCdEf1234"))
    assert req["agentRuntimeName"] == "shopassist_support_mcp"
    assert req["protocolConfiguration"] == {"serverProtocol": "MCP"}
    code = req["agentRuntimeArtifact"]["codeConfiguration"]
    assert code == {"code": {"s3": {"bucket": f"shopassist-artifacts-{ACCOUNT}-{REGION}",
                                    "prefix": "artifacts/support_mcp.zip", "versionId": "v-mcp"}},
                    "runtime": "PYTHON_3_12", "entryPoint": ["server.py"]}
    jwt = req["authorizerConfiguration"]["customJWTAuthorizer"]
    assert jwt == {"discoveryUrl": DISCOVERY, "allowedClients": ["m2mclientid000000000000000"]}
    assert req["environmentVariables"] == {"STORE": "dynamodb", "TICKETS_TABLE": "shopassist-tickets",
                                           "AWS_REGION": REGION}


def test_agent_runtime_request(ctx):
    rc, req = M["runtime_common"], M["agent_runtime"].build_create_request(ctx)
    validate("bedrock-agentcore-control", "create_agent_runtime", req)
    validate("bedrock-agentcore-control", "update_agent_runtime", rc.as_update_request(req, "shopassist_agent-AbCdEf1234"))
    assert req["agentRuntimeName"] == "shopassist_agent"
    assert req["protocolConfiguration"] == {"serverProtocol": "HTTP"}
    assert req["agentRuntimeArtifact"]["codeConfiguration"]["entryPoint"] == ["app.py"]
    # design.md section 8, layer 1: users client only, plus the cognito:groups custom claim rule.
    jwt = req["authorizerConfiguration"]["customJWTAuthorizer"]
    assert jwt["allowedClients"] == ["usersclientid0000000000000"]
    assert jwt["customClaims"] == [{
        "inboundTokenClaimName": "cognito:groups",
        "inboundTokenClaimValueType": "STRING_ARRAY",
        "authorizingClaimMatchValue": {"claimMatchValue": {"matchValueStringList": ["shop-admin", "shop-viewer"]},
                                       "claimMatchOperator": "CONTAINS_ANY"},
    }]
    assert req["requestHeaderConfiguration"] == {"requestHeaderAllowlist": ["Authorization"]}
    env = req["environmentVariables"]
    assert env["GATEWAY_AUTH_MODE"] == "passthrough"
    assert env["GATEWAY_URL"].endswith(".gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
    assert env["OAUTH_PROVIDER_NAME"] == "shopassist-cognito" and env["OAUTH_SCOPES"] == "shopassist/invoke"
    assert env["SKILLS_S3_URI"] == f"s3://shopassist-artifacts-{ACCOUNT}-{REGION}/skills/"
    assert env["MODEL_ID"] == common.DEFAULT_MODEL_ID and env["AWS_REGION"] == REGION
    assert req["tags"] == {"project": "shopassist"}


def test_invocation_url():
    url = M["runtime_common"].invocation_url(REGION, MCP_ARN)
    head, _, tail = url.partition("/runtimes/")
    assert head == "https://bedrock-agentcore.us-east-1.amazonaws.com"
    encoded, _, query = tail.partition("/invocations?")
    assert "/" not in encoded and ":" not in encoded and unquote(encoded) == MCP_ARN
    assert query == "qualifier=DEFAULT"


# ------------------------------------------------------------------ Gateway, targets, Policy


def test_gateway_requests(ctx):
    gw = M["gateway"]
    req = gw.build_create_gateway_request("shopassist-gateway", f"arn:aws:iam::{ACCOUNT}:role/g", DISCOVERY,
                                          ["users", "m2m"], ENGINE_ARN, ctx.names.tags)
    validate("bedrock-agentcore-control", "create_gateway", req)
    validate("bedrock-agentcore-control", "update_gateway", gw.as_update_gateway_request(req, GATEWAY_ID))
    assert req["protocolType"] == "MCP" and req["authorizerType"] == "CUSTOM_JWT"
    assert req["authorizerConfiguration"]["customJWTAuthorizer"]["allowedClients"] == ["users", "m2m"]
    assert req["policyEngineConfiguration"] == {"arn": ENGINE_ARN, "mode": "ENFORCE"}
    assert "protocolConfiguration" not in req  # no semantic search
    assert gw.gateway_drift(req, dict(req, authorizerConfiguration={"customJWTAuthorizer": {
        "discoveryUrl": DISCOVERY, "allowedClients": ["m2m", "users"]}})) == []
    assert gw.gateway_drift(req, dict(req, policyEngineConfiguration={"arn": ENGINE_ARN, "mode": "LOG_ONLY"})) == [
        "policyEngineConfiguration"]


def test_gateway_target_requests(ctx):
    gw, rc = M["gateway"], M["runtime_common"]
    tools = gw.load_tool_schema()
    assert {t["name"] for t in tools} == {"get_order", "list_customer_orders", "check_return_eligibility",
                                          "create_return"}
    orders = gw.build_orders_target_request(GATEWAY_ID, LAMBDA_ARN, tools)
    support = gw.build_support_target_request(GATEWAY_ID, rc.invocation_url(REGION, MCP_ARN), PROVIDER_ARN,
                                              ["shopassist/invoke"])
    for req in (orders, support):
        validate("bedrock-agentcore-control", "create_gateway_target", req)
        validate("bedrock-agentcore-control", "update_gateway_target", gw.as_update_target_request(req, "ABCDEFGHIJ"))
        assert not gw.target_differs(req, req)
    assert orders["name"] == "orders" and support["name"] == "support"
    assert orders["credentialProviderConfigurations"] == [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]
    oauth = support["credentialProviderConfigurations"][0]
    assert oauth["credentialProviderType"] == "OAUTH"
    assert oauth["credentialProvider"]["oauthCredentialProvider"] == {
        "providerArn": PROVIDER_ARN, "scopes": ["shopassist/invoke"], "grantType": "CLIENT_CREDENTIALS"}
    assert support["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"].endswith("/invocations?qualifier=DEFAULT")
    changed = gw.build_support_target_request(GATEWAY_ID, "https://example.com/mcp", PROVIDER_ARN, ["shopassist/invoke"])
    assert gw.target_differs(support, changed)
    validate("bedrock-agentcore-control", "synchronize_gateway_targets", gw.build_synchronize_request(GATEWAY_ID, ["t"]))


def test_policy_requests():
    pol = M["policy"]
    validate("bedrock-agentcore-control", "create_policy_engine",
             pol.build_create_engine_request("shopassist_policy_engine", {"project": "shopassist"}))
    rendered = pol.load_policies(GATEWAY_ARN, "m2mclientid")
    assert set(rendered) == {"admin_all_tools", "m2m_dev_client", "viewer_read_only"}
    for name, statement in rendered.items():
        assert "{{" not in statement
        assert statement.startswith("//") or statement.startswith("permit")
        req = pol.build_create_policy_request(ENGINE_ID, name, statement)
        validate("bedrock-agentcore-control", "create_policy", req)
        assert req["definition"] == {"cedar": {"statement": statement}}
        validate("bedrock-agentcore-control", "update_policy", pol.build_update_policy_request(ENGINE_ID, "admin_all_tools-abcdefghij", statement))
    assert f'AgentCore::Gateway::"{GATEWAY_ARN}"' in rendered["viewer_read_only"]
    assert '"m2mclientid"' in rendered["m2m_dev_client"]
    assert "orders___create_return" not in rendered["viewer_read_only"]


# ------------------------------------------------------------------ Budget, outputs


def test_budget_request(ctx):
    req = M["budget"].build_create_budget_request(ACCOUNT, ctx.names.budget, "me@example.com", 5, ctx.names.tags)
    validate("budgets", "create_budget", req)
    assert req["Budget"]["BudgetName"] == "shopassist-monthly"


def test_outputs_contract(ctx):
    out = M["outputs"].build_outputs(ctx.state.data)
    design_keys = {"region", "account_id", "artifacts_bucket", "skills_s3_uri", "agent_runtime_arn",
                   "support_mcp_runtime_arn", "gateway_id", "gateway_url", "cognito_user_pool_id",
                   "cognito_client_id", "cognito_token_url", "oauth_provider_name", "deployed_by",
                   "cognito_users_client_id", "policy_engine_id"}
    assert set(out) == design_keys == set(M["outputs"].OUTPUT_KEYS)
    assert out["deployed_by"] == "boto3"
    assert out["cognito_client_id"] == "m2mclientid000000000000000"
    assert out["cognito_users_client_id"] == "usersclientid0000000000000"
    assert all(isinstance(v, str) and v for v in out.values())


def test_terraform_outputs_have_the_same_keys():
    tf = (ROOT / "infra" / "terraform" / "outputs.tf").read_text(encoding="utf-8")
    for key in M["outputs"].OUTPUT_KEYS:
        assert f'output "{key}"' in tf, key


# ------------------------------------------------------------------ statuses, waiters, paginators


def test_status_names_exist_in_service_model():
    svc = "bedrock-agentcore-control"
    rc, pol, gw = M["runtime_common"], M["policy"], M["gateway"]
    runtime = enum(svc, "AgentRuntimeStatus")
    assert "READY" in runtime and rc.FAILED <= runtime
    assert {"CREATING", "UPDATING"} <= runtime
    assert "ACTIVE" in enum(svc, "PolicyEngineStatus") and pol.ENGINE_FAILED <= enum(svc, "PolicyEngineStatus")
    assert "ACTIVE" in enum(svc, "PolicyStatus") and pol.POLICY_FAILED <= enum(svc, "PolicyStatus")
    assert "READY" in enum(svc, "GatewayStatus") and gw.GATEWAY_FAILED <= enum(svc, "GatewayStatus")
    assert "READY" in enum(svc, "TargetStatus") and gw.TARGET_FAILED <= enum(svc, "TargetStatus")
    provider_status = client(svc).meta.service_model.operation_model(
        "GetOauth2CredentialProvider").output_shape.members["status"].enum
    assert {"READY", "CREATE_FAILED", "UPDATE_FAILED"} <= set(provider_status)


def test_waiters_and_paginators_exist():
    assert {"policy_active", "policy_engine_active", "policy_deleted", "policy_engine_deleted"} <= set(
        client("bedrock-agentcore-control").waiter_names)
    assert {"table_exists", "table_not_exists"} <= set(client("dynamodb").waiter_names)
    assert {"function_active_v2", "function_updated_v2"} <= set(client("lambda").waiter_names)
    assert "role_exists" in client("iam").waiter_names
    assert "bucket_exists" in client("s3").waiter_names
    ac = client("bedrock-agentcore-control")
    assert ac.can_paginate("list_gateways") and ac.can_paginate("list_gateway_targets")
    for op in ("list_agent_runtimes", "list_policy_engines", "list_policies"):
        assert hasattr(ac, op)
    idp = client("cognito-idp")
    assert idp.can_paginate("list_user_pools") and idp.can_paginate("list_user_pool_clients")
    assert client("logs").can_paginate("describe_log_groups")
    assert client("s3").can_paginate("list_object_versions")


def test_status_fields_read_by_the_code_exist():
    m = client("bedrock-agentcore-control").meta.service_model
    for op in ("GetGateway", "GetGatewayTarget", "GetPolicyEngine", "GetPolicy"):
        out = m.operation_model(op).output_shape.members
        assert "status" in out and out["statusReasons"].type_name == "list", op
    assert "failureReason" in m.operation_model("GetAgentRuntime").output_shape.members
    assert {"gatewayUrl", "gatewayArn"} <= set(m.operation_model("GetGateway").output_shape.members)
    assert "clientSecretArn" in m.operation_model("CreateOauth2CredentialProvider").output_shape.members


# ------------------------------------------------------------------ CLI and step order


def test_deploy_cli_and_order():
    dep = M["deploy"]
    args = dep.parse_args(["--region", "us-east-1", "--prefix", "shopassist", "--model-id", "m", "--skip-seed",
                           "--budget-email", "me@example.com"])
    titles = [t for t, _ in dep.steps(args)]
    idx = {key: next(i for i, t in enumerate(titles) if key in t)
           for key in ("Policy engine", "IAM", "support MCP", "Gateway", "Cedar", "agent", "Budget", "outputs")}
    assert idx["Policy engine"] < idx["IAM"] < idx["support MCP"] < idx["Gateway"] < idx["Cedar"] < idx["agent"]
    assert titles[-1] == "Deployment outputs" and idx["Budget"] == len(titles) - 2
    assert "Budget" not in " ".join(t for t, _ in dep.steps(dep.parse_args([]))).replace("AWS Budget", "")
    with pytest.raises(SystemExit):
        dep.parse_args(["--prefix", "Bad-Prefix"])


def test_destroy_order():
    titles = [t for t, _ in M["destroy"].steps()]
    order = ["Cedar policies", "Gateway targets", "Gateway", "Policy engine", "Agent runtime", "Support MCP runtime",
             "OAuth2 credential provider", "Lambda orders tools", "DynamoDB tables", "Cognito domain and user pool",
             "Log groups", "IAM roles", "S3 bucket (all object versions)"]
    assert titles[:len(order)] == order


def test_deploy_fails_fast_without_build_artifacts(monkeypatch, tmp_path, capsys):
    dep = M["deploy"]
    monkeypatch.setattr(dep, "ARTIFACTS", {"agent": tmp_path / "dist" / "agent.zip"})
    monkeypatch.setattr(dep, "REPO_ROOT", tmp_path)
    with pytest.raises(SystemExit) as exc:
        dep.check_artifacts()
    assert "dist/agent.zip" in str(exc.value) and "uv run python scripts/build.py" in str(exc.value)
