"""AgentCore Runtime `<prefix>_agent`: the generic Strands agent (`app.py`, protocol HTTP).

What this file does
    Deploy step 11. Hosts the ShopAssist agent on AgentCore Runtime (managed hosting for agent code), and
    decides who is allowed to talk to it.

Where it fits
    It runs late because it needs the Gateway address (step 9), the skills location in S3 (step 1), the
    Cognito login details (step 2) and its IAM role (step 5). Users call this runtime through
    `scripts/invoke_agent.py`; the agent then calls the tools through the Gateway.

Per-user access (design.md section 8) is enforced here *before any agent code runs*:

* **JWT authorizer**: the caller must present a Cognito access token from the `<prefix>-users` client,
  and a **custom claim rule** requires `cognito:groups` (a string array) to contain `shop-admin` or
  `shop-viewer`. carol (no group) gets HTTP 403 from the Runtime. With a JWT authorizer the runtime
  no longer accepts SigV4, so clients call the HTTPS invocation URL with `Authorization: Bearer`.
* **requestHeaderConfiguration** allowlists `Authorization`, so the agent receives the user's token and
  (`GATEWAY_AUTH_MODE=passthrough`) forwards it to the Gateway, where the Cedar policies apply per user.

Step by step
    1. Build the create request: code zip location, IAM role, login rules, settings for the agent.
    2. Create the runtime, or update it if anything changed (shared logic in `runtime_common.py`).
    3. Set 7-day log retention, and save the runtime ID, ARN and invocation URL in state.json.

Key terms
    JWT (JSON Web Token): a signed login token saying who the user is and which groups they belong to.
    Claim: one field inside a JWT, e.g. `cognito:groups` = ["shop-admin"].
    SigV4: AWS's own way of signing requests with AWS access keys (switched off here in favour of JWTs).
    Bearer token: a token sent in the `Authorization: Bearer <token>` header of a web request.
    Environment variable: a named setting the hosted code reads at start-up (e.g. GATEWAY_URL).
"""

from __future__ import annotations

from common import Context, info
from steps import runtime_common as rc

# The file inside agent.zip that AgentCore starts.
ENTRY_POINT = "app.py"
# Only members of these Cognito groups may use the agent at all (alice and bob, not carol).
ALLOWED_GROUPS = ["shop-admin", "shop-viewer"]


def build_environment(ctx: Context) -> dict[str, str]:
    """Settings handed to the agent code: which model, where the Gateway and skills are, how to log in.

    GATEWAY_AUTH_MODE "passthrough" means the agent forwards the user's own token to the Gateway, so
    the Gateway's rulebook can decide per user (alice may create returns, bob may only look).
    """
    n = ctx.names
    return {
        "MODEL_ID": ctx.model_id,
        "AWS_REGION": ctx.region,
        "GATEWAY_URL": ctx.state.section("gateway")["url"],
        "GATEWAY_AUTH_MODE": "passthrough",
        "OAUTH_PROVIDER_NAME": n.oauth_provider,
        "OAUTH_SCOPES": n.scope,
        "SKILLS_S3_URI": ctx.state.section("s3")["skills_s3_uri"],
    }


def build_group_claim_rule(groups: list[str]) -> dict:
    """`cognito:groups` CONTAINS_ANY of `groups` (claim value type STRING_ARRAY).

    In plain words: "let the caller in only if their token lists at least one of these groups".
    Cognito puts the user's groups in the token as a list, hence STRING_ARRAY and CONTAINS_ANY.
    """
    return {
        "inboundTokenClaimName": "cognito:groups",
        "inboundTokenClaimValueType": "STRING_ARRAY",
        "authorizingClaimMatchValue": {
            "claimMatchValue": {"matchValueStringList": groups},
            "claimMatchOperator": "CONTAINS_ANY",
        },
    }


def build_create_request(ctx: Context) -> dict:
    """The full CreateAgentRuntime request for the agent.

    Important fields:
    - agentRuntimeArtifact: where the code is (agent.zip in S3, pinned to one file version) and how to
      start it (Python 3.12, app.py). See `runtime_common.build_code_artifact`.
    - roleArn: the IAM role the agent runs as (call the Bedrock model, read skills from S3, write logs).
    - networkConfiguration PUBLIC: the runtime can reach the internet and AWS services without a private
      network (VPC), which keeps the PoC simple.
    - protocolConfiguration HTTP: the agent is a plain web service (not an MCP tool server).
    - authorizerConfiguration: the login check. Only tokens from the users app client are accepted, and
      the group rule above must pass.
    - requestHeaderConfiguration: lets the `Authorization` header (the user's token) through to the code.
      Without it the Runtime would strip the header and the agent could not pass the token on.
    - environmentVariables: the settings from `build_environment`.
    """
    s3 = ctx.state.section("s3")
    art = s3["artifacts"]["agent"]
    cog = ctx.state.section("cognito")
    return {
        "agentRuntimeName": ctx.names.agent_runtime,
        "description": "Generic skills-driven Strands agent",
        "agentRuntimeArtifact": rc.build_code_artifact(s3["bucket"], art["key"], art["version_id"], ENTRY_POINT),
        "roleArn": ctx.state.section("iam")["roles"]["agent-runtime"]["arn"],
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "authorizerConfiguration": rc.build_jwt_authorizer(
            cog["discovery_url"], [cog["users_client_id"]], [build_group_claim_rule(ALLOWED_GROUPS)]),
        "requestHeaderConfiguration": {"requestHeaderAllowlist": ["Authorization"]},
        "environmentVariables": build_environment(ctx),
        "tags": ctx.names.tags,
    }


def deploy(ctx: Context) -> None:
    """Create or update the agent runtime, set log retention, and record its address."""
    # Step 1 + 2: create, update or keep the runtime, and wait until it is READY.
    runtime, _ = rc.ensure_runtime(ctx, build_create_request(ctx))
    # Step 3: keep its logs for 7 days only.
    rc.ensure_log_retention(ctx, runtime["agentRuntimeId"])
    # Step 4: save the details; the invocation URL is what scripts/invoke_agent.py calls.
    arn = runtime["agentRuntimeArn"]
    ctx.state.put("agent_runtime", id=runtime["agentRuntimeId"], arn=arn,
                  version=runtime["agentRuntimeVersion"], url=rc.invocation_url(ctx.region, arn))
    info(f"invocation URL: {rc.invocation_url(ctx.region, arn)}")


def destroy(ctx: Context) -> None:
    """Delete the agent runtime and its log groups."""
    rc.delete_runtime(ctx, ctx.names.agent_runtime)
    ctx.state.drop("agent_runtime")
