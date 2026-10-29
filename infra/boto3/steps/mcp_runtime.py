"""AgentCore Runtime `<prefix>_support_mcp`: the FastMCP `support` server (tickets + FAQ).

What this file does
    Deploy step 8. Hosts the support tool server (create and look up support tickets, answer FAQ
    questions) on AgentCore Runtime, and makes sure only the Gateway can call it.

Where it fits
    Needs the code zip (step 1), the Cognito M2M client (step 2), its IAM role (step 5) and the tickets
    table (step 6). It must be READY before step 9, because when the Gateway's `support` target is
    created, the Gateway immediately asks this server for its list of tools.

`serverProtocol: MCP` tells the Runtime that the code is a stateless streamable-HTTP MCP server on
`0.0.0.0:8000/mcp`. Clients reach it at `.../runtimes/<urlencoded ARN>/invocations?qualifier=DEFAULT`.

Inbound auth is a **JWT authorizer** (Cognito discovery URL, allowedClients = the M2M client): only the
Gateway, holding an M2M token from AgentCore Identity, can call it. Nobody calls it with SigV4.

Step by step
    1. Build the create request (code, role, MCP protocol, login rule, settings).
    2. Create or update the runtime and wait until READY (`runtime_common.ensure_runtime`).
    3. Tell the Gateway step whether the server changed, so it can refresh its cached tool list.
    4. Set 7-day log retention and save the runtime ID, ARN and URL in state.json.

Key terms
    FastMCP: a Python library for writing MCP tool servers.
    MCP (Model Context Protocol): a standard way for an AI agent to discover and call tools.
    Streamable HTTP: the MCP variant that runs over ordinary web requests.
    JWT authorizer: a gate that only lets in requests carrying a valid Cognito login token.
"""

from __future__ import annotations

from common import Context, info
from steps import runtime_common as rc

# The file inside support_mcp.zip that AgentCore starts.
ENTRY_POINT = "server.py"


def build_environment(ctx: Context) -> dict[str, str]:
    """Settings the server code reads: use DynamoDB, which tickets table, and which region."""
    return {"STORE": "dynamodb", "TICKETS_TABLE": ctx.names.tickets_table, "AWS_REGION": ctx.region}


def build_create_request(ctx: Context) -> dict:
    """The full CreateAgentRuntime request for the support MCP server.

    - agentRuntimeArtifact.codeConfiguration: support_mcp.zip in S3 (pinned version), Python 3.12, server.py.
    - protocolConfiguration MCP: the code is an MCP tool server (not a plain web app).
    - authorizerConfiguration: only tokens issued to the M2M client are accepted (that is, the Gateway).
    """
    s3 = ctx.state.section("s3")
    art = s3["artifacts"]["support_mcp"]
    cog = ctx.state.section("cognito")
    return {
        "agentRuntimeName": ctx.names.mcp_runtime,
        "description": "ShopAssist support tools (FastMCP, stateless streamable HTTP)",
        "agentRuntimeArtifact": rc.build_code_artifact(s3["bucket"], art["key"], art["version_id"], ENTRY_POINT),
        "roleArn": ctx.state.section("iam")["roles"]["mcp-runtime"]["arn"],
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "MCP"},
        "authorizerConfiguration": rc.build_jwt_authorizer(cog["discovery_url"], [cog["m2m_client_id"]]),
        "environmentVariables": build_environment(ctx),
        "tags": ctx.names.tags,
    }


def deploy(ctx: Context) -> None:
    """Create or update the support MCP runtime, and record its address for the Gateway."""
    # Step 1 + 2: create, update or keep it, and wait for READY.
    runtime, changed = rc.ensure_runtime(ctx, build_create_request(ctx))
    # Step 3: pass a note to the Gateway step (later in this same run).
    # The Gateway caches the tool list of an MCP-server target; gateway.py re-syncs it when this is set.
    ctx.flags["mcp_runtime_updated"] = changed
    # Step 4: log retention, then save; the invocation URL becomes the Gateway `support` target endpoint.
    rc.ensure_log_retention(ctx, runtime["agentRuntimeId"])
    arn = runtime["agentRuntimeArn"]
    ctx.state.put("mcp_runtime", id=runtime["agentRuntimeId"], arn=arn,
                  version=runtime["agentRuntimeVersion"], url=rc.invocation_url(ctx.region, arn))
    info(f"invocation URL: {rc.invocation_url(ctx.region, arn)}")


def destroy(ctx: Context) -> None:
    """Delete the support MCP runtime and its log groups."""
    rc.delete_runtime(ctx, ctx.names.mcp_runtime)
    ctx.state.drop("mcp_runtime")
