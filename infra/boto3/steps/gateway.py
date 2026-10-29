"""AgentCore Gateway `<prefix>-gateway`: the single MCP endpoint the agent talks to.

What this file does
    Deploy step 9. Creates the Gateway (the single front door to all tools) and connects two tool
    sources ("targets") to it: `orders` (the Lambda function) and `support` (the support MCP server).

Where it fits
    Needs the Cognito details (step 2), the credential provider (step 3), the policy engine (step 4), the
    Gateway IAM role (step 5), the Lambda (step 7) and a READY support MCP runtime (step 8). After it, the
    Cedar policies (step 10) are checked against the Gateway's tools, and the agent (step 11) is given the
    Gateway URL. At run time: agent -> Gateway -> Lambda or support MCP server.

A Gateway is a managed MCP server that aggregates *targets* and exposes their tools as
`<target>___<tool>` (three underscores). It has three security edges, all configured here:

* **Inbound** (who may call the Gateway): a `CUSTOM_JWT` authorizer on the Cognito discovery URL with
  `allowedClients = [users client, m2m client]` (design.md section 8). The deployed agent forwards the
  *user's* token; the M2M client is kept so local development works.
* **Authorization** (which tools each caller may list and call): `policyEngineConfiguration` attaches the
  Cedar policy engine (`policy.py`) in `ENFORCE` mode. Default deny, and `tools/list` is filtered per caller.
* **Outbound** (how the Gateway calls each target), set per target:
  - `orders`: a **Lambda target** with an *inline tool schema* (a Lambda has no `tools/list`, so the
    Gateway must be told the tool names and input schemas), called with the Gateway's IAM role
    (`GATEWAY_IAM_ROLE`).
  - `support`: an **MCP server target**, the support MCP runtime's HTTPS invocation URL. The Gateway
    lists the server's tools itself (at create time, and again on SynchronizeGatewayTargets) and
    authenticates with an OAuth2 client_credentials token from the AgentCore Identity credential
    provider `<prefix>-cognito` (scope `<prefix>/invoke`).

`exceptionLevel: DEBUG` returns target error details to the caller, which is handy while learning (same as
the Terraform path; drop it in production). No `searchType`, so semantic tool search stays off.

Order matters (design.md section 8): policy engine -> gateway -> targets -> policies. Targets are created
one at a time, because concurrent changes on one Gateway can conflict.

Step by step
    1. Create the Gateway, or update it if its settings drifted, or recreate it if it is FAILED.
    2. Wait until it is READY.
    3. Create or update the `orders` target (Lambda + tool list from tools_schema.json), wait for READY.
    4. Create or update the `support` target (MCP server URL + OAuth login), wait for READY. If the
       support server's code changed, ask the Gateway to re-read its tool list.
    5. Save the Gateway ID, ARN, URL and target IDs in state.json.

Key terms
    MCP (Model Context Protocol): a standard way for an AI agent to discover (`tools/list`) and call
    (`tools/call`) tools.
    Target: one tool source behind the Gateway.
    Tool schema: the name, description and allowed inputs of each tool.
    Drift: a difference between the settings we want and the settings AWS currently has.
    ENFORCE mode: the rulebook really blocks calls (LOG_ONLY would only record what it would block).
"""

from __future__ import annotations

import json

from common import (TOOLS_SCHEMA_PATH, Context, OK, call_with_iam_retry, ignore_not_found, info, is_not_found,
                    wait_for_status)
from steps import runtime_common as rc

# Statuses that mean "this went wrong", so waiting stops with an error instead of waiting forever.
GATEWAY_FAILED = {"FAILED", "UPDATE_UNSUCCESSFUL"}
# *_PENDING_AUTH only happens with 3-legged OAuth (AUTHORIZATION_CODE); with client_credentials it means
# something is misconfigured, so it is treated as a failure instead of waiting forever.
TARGET_FAILED = {"FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL", "CREATE_PENDING_AUTH",
                 "UPDATE_PENDING_AUTH", "SYNCHRONIZE_PENDING_AUTH"}

# Fixed names and descriptions. The target names become the tool name prefixes the agent sees,
# e.g. `orders___get_order` and `support___create_ticket`, so they must match the skills' mcp-servers.
GATEWAY_DESCRIPTION = "Single MCP endpoint for the ShopAssist agent"
ORDERS_TARGET = "orders"
SUPPORT_TARGET = "support"
ORDERS_DESCRIPTION = "Order lookup and returns (Lambda)"
SUPPORT_DESCRIPTION = "Tickets and FAQ (FastMCP on AgentCore Runtime)"


# ------------------------------------------------------------------ request builders (pure)


def load_tool_schema(path=TOOLS_SCHEMA_PATH) -> list[dict]:
    """`tools_schema.json` is already in the Gateway inline format: [{name, description, inputSchema}]."""
    return json.loads(path.read_text(encoding="utf-8"))


def build_create_gateway_request(name: str, role_arn: str, discovery_url: str, allowed_clients: list[str],
                                 policy_engine_arn: str, tags: dict[str, str]) -> dict:
    """The CreateGateway request.

    Important fields:
    - roleArn: the IAM role the Gateway uses to call the Lambda, fetch OAuth tokens and ask the rulebook.
    - protocolType MCP: the Gateway speaks MCP to the agent.
    - authorizerType CUSTOM_JWT + authorizerConfiguration.customJWTAuthorizer: only accept Cognito tokens
      (checked via the discovery URL) issued to one of `allowed_clients` (the users and m2m clients).
    - policyEngineConfiguration: attach the Cedar rulebook and enforce it.
    - exceptionLevel DEBUG: return detailed error messages (useful in a PoC, not in production).
    """
    return {
        "name": name,
        "description": GATEWAY_DESCRIPTION,
        "roleArn": role_arn,
        "protocolType": "MCP",
        "authorizerType": "CUSTOM_JWT",
        "authorizerConfiguration": rc.build_jwt_authorizer(discovery_url, allowed_clients),
        "policyEngineConfiguration": {"arn": policy_engine_arn, "mode": "ENFORCE"},  # or LOG_ONLY to dry-run
        "exceptionLevel": "DEBUG",
        "tags": tags,
    }


def as_update_gateway_request(create_request: dict, gateway_id: str) -> dict:
    """UpdateGateway takes the full desired spec again (name included), minus the tags."""
    req = {k: v for k, v in create_request.items() if k != "tags"}
    req["gatewayIdentifier"] = gateway_id
    return req


def build_orders_target_request(gateway_id: str, lambda_arn: str, tools: list[dict]) -> dict:
    """CreateGatewayTarget request for `orders`.

    - targetConfiguration.mcp.lambda: the Lambda to call, plus the tool list written out in full
      (`inlinePayload`), because a Lambda cannot describe its own tools.
    - credentialProviderConfigurations GATEWAY_IAM_ROLE: the Gateway calls the Lambda using its own IAM
      role (no token needed; AWS permissions decide).
    """
    return {
        "gatewayIdentifier": gateway_id,
        "name": ORDERS_TARGET,
        "description": ORDERS_DESCRIPTION,
        "targetConfiguration": {"mcp": {"lambda": {"lambdaArn": lambda_arn, "toolSchema": {"inlinePayload": tools}}}},
        "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    }


def build_support_target_request(gateway_id: str, endpoint: str, provider_arn: str, scopes: list[str]) -> dict:
    """CreateGatewayTarget request for `support`.

    - targetConfiguration.mcp.mcpServer.endpoint: the web address of the support MCP runtime. The
      Gateway asks this server for its tool list itself.
    - credentialProviderConfigurations OAUTH: before calling the server, the Gateway gets a login token
      from the AgentCore Identity provider (`providerArn`), using the machine login (CLIENT_CREDENTIALS)
      with the `<prefix>/invoke` scope. The support runtime only accepts that token.
    """
    return {
        "gatewayIdentifier": gateway_id,
        "name": SUPPORT_TARGET,
        "description": SUPPORT_DESCRIPTION,
        "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": endpoint}}},
        "credentialProviderConfigurations": [{
            "credentialProviderType": "OAUTH",
            "credentialProvider": {"oauthCredentialProvider": {
                "providerArn": provider_arn,
                "scopes": scopes,
                "grantType": "CLIENT_CREDENTIALS",
            }},
        }],
    }


def as_update_target_request(create_request: dict, target_id: str) -> dict:
    """UpdateGatewayTarget takes the same fields as the create request, plus the target's ID."""
    req = dict(create_request)
    req["targetId"] = target_id
    return req


def build_synchronize_request(gateway_id: str, target_ids: list[str]) -> dict:
    """SynchronizeGatewayTargets request: "re-read the tool list of these MCP server targets"."""
    return {"gatewayIdentifier": gateway_id, "targetIdList": target_ids}


# ------------------------------------------------------------------ drift detection (pure)


def gateway_drift(desired: dict, current: dict) -> list[str]:
    """Fields we manage whose current value differs. Lists are compared order-insensitively.

    Used to skip the (slow) UpdateGateway call when nothing changed.
    """
    def jwt(cfg: dict) -> dict:
        """The JWT settings with the client list sorted, so order alone doesn't count as a change."""
        j = dict((cfg or {}).get("customJWTAuthorizer", {}))
        j["allowedClients"] = sorted(j.get("allowedClients", []))
        return j

    diffs = [k for k in ("roleArn", "description", "authorizerType", "exceptionLevel")
             if desired.get(k) != current.get(k)]
    if jwt(desired["authorizerConfiguration"]) != jwt(current.get("authorizerConfiguration")):
        diffs.append("authorizerConfiguration")
    if desired["policyEngineConfiguration"] != current.get("policyEngineConfiguration"):
        diffs.append("policyEngineConfiguration")
    return diffs


def _target_fingerprint(target: dict) -> str:
    """What we care about in a target, in a form that ignores key order and server-added defaults."""
    mcp = target.get("targetConfiguration", {}).get("mcp", {})
    fp: dict = {"description": target.get("description")}
    if "lambda" in mcp:
        fp["lambdaArn"] = mcp["lambda"].get("lambdaArn")
        fp["tools"] = mcp["lambda"].get("toolSchema", {}).get("inlinePayload")
    if "mcpServer" in mcp:
        fp["endpoint"] = mcp["mcpServer"].get("endpoint")
    creds = []
    for c in target.get("credentialProviderConfigurations", []):
        oauth = c.get("credentialProvider", {}).get("oauthCredentialProvider", {})
        creds.append([c.get("credentialProviderType"), oauth.get("providerArn"), sorted(oauth.get("scopes", []))])
    fp["creds"] = creds
    return json.dumps(fp, sort_keys=True)


def target_differs(desired: dict, current: dict) -> bool:
    """True when the target in AWS no longer matches what we want (so it needs an update)."""
    return _target_fingerprint(desired) != _target_fingerprint(current)


# ------------------------------------------------------------------ helpers


def find_gateway(ac, name: str) -> dict | None:
    """Find our gateway by name (AWS API ListGateways). None if it does not exist."""
    for page in ac.get_paginator("list_gateways").paginate():
        for gw in page["items"]:
            if gw["name"] == name:
                return gw
    return None


def list_targets(ac, gateway_id: str) -> dict[str, dict]:
    """All targets of a gateway, by name (AWS API ListGatewayTargets)."""
    return {t["name"]: t
            for page in ac.get_paginator("list_gateway_targets").paginate(gatewayIdentifier=gateway_id)
            for t in page["items"]}


def _gateway_status(ac, gateway_id: str):
    """(status, reasons) of the gateway via AWS API GetGateway; (None, "") once it is deleted."""
    try:
        g = ac.get_gateway(gatewayIdentifier=gateway_id)
        return g["status"], "; ".join(g.get("statusReasons", []))
    except Exception as err:  # noqa: BLE001
        if is_not_found(err):
            return None, ""
        raise


def _target_status(ac, gateway_id: str, target_id: str):
    """(status, reasons) of one target via AWS API GetGatewayTarget; (None, "") once it is deleted."""
    try:
        t = ac.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        return t["status"], "; ".join(t.get("statusReasons", []))
    except Exception as err:  # noqa: BLE001
        if is_not_found(err):
            return None, ""
        raise


def wait_gateway_ready(ac, gateway_id: str, what: str) -> None:
    """Poll the gateway until READY (up to 10 minutes); stop with an error if it fails or disappears."""
    wait_for_status(what, lambda: _gateway_status(ac, gateway_id),
                    ready={"READY"}, failed=GATEWAY_FAILED | {"DELETED"}, timeout=600)


def wait_target_ready(ac, gateway_id: str, target_id: str, what: str) -> None:
    """Poll a target until READY (up to 10 minutes); stop with an error if it fails or disappears."""
    wait_for_status(what, lambda: _target_status(ac, gateway_id, target_id),
                    ready={"READY"}, failed=TARGET_FAILED | {"DELETED"}, timeout=600)


# ------------------------------------------------------------------ deploy


def _ensure_gateway(ctx: Context) -> dict:
    """Get-or-create-or-update the gateway itself, and return its details once READY."""
    ac = ctx.client("bedrock-agentcore-control")
    n = ctx.names
    cog = ctx.state.section("cognito")
    # Step 1: build the desired settings from earlier steps' results (role, Cognito, policy engine).
    create_req = build_create_gateway_request(
        n.gateway,
        ctx.state.section("iam")["roles"]["gateway"]["arn"],
        cog["discovery_url"],
        [cog["users_client_id"], cog["m2m_client_id"]],
        ctx.state.section("policy")["engine_arn"],
        n.tags,
    )
    # Step 2: look for an existing gateway. A FAILED one cannot be repaired, so delete it and start over.
    summary = find_gateway(ac, n.gateway)
    if summary and summary["status"] == "FAILED":
        current = ac.get_gateway(gatewayIdentifier=summary["gatewayId"])
        info(f"gateway {n.gateway} is FAILED ({'; '.join(current.get('statusReasons', []))}); recreating")
        _delete_gateway(ac, summary["gatewayId"])
        summary = None

    if summary is None:
        # Step 3a: none exists, so create it (AWS API CreateGateway) and wait for READY.
        # CreateGateway checks the role right away; a just-created role may not be assumable yet.
        resp = call_with_iam_retry(ac.create_gateway, **create_req)
        gateway_id = resp["gatewayId"]
        info(f"{OK} create requested: gateway {gateway_id}")
        wait_gateway_ready(ac, gateway_id, f"gateway {n.gateway}")
    else:
        # Step 3b: it exists. Let an unfinished change from a previous run finish first.
        gateway_id = summary["gatewayId"]
        if summary["status"] in ("CREATING", "UPDATING"):
            wait_gateway_ready(ac, gateway_id, f"gateway {n.gateway} (in progress from an earlier run)")
        # Then compare its settings with the desired ones, and update only if something differs
        # (AWS API UpdateGateway) or it is not READY.
        current = ac.get_gateway(gatewayIdentifier=gateway_id)
        drift = gateway_drift(create_req, current)
        if drift or current["status"] != "READY":
            info(f"gateway {n.gateway} needs update: {', '.join(drift) or 'status ' + current['status']}")
            call_with_iam_retry(ac.update_gateway, **as_update_gateway_request(create_req, gateway_id))
            wait_gateway_ready(ac, gateway_id, f"gateway {n.gateway}")
        else:
            info(f"gateway {n.gateway} exists ({gateway_id}), up to date")
    # Step 4: read the final details and save them; the URL is what the agent connects to.
    gw = ac.get_gateway(gatewayIdentifier=gateway_id)
    ctx.state.put("gateway", id=gateway_id, arn=gw["gatewayArn"], url=gw["gatewayUrl"], name=n.gateway)
    return gw


def _ensure_target(ctx: Context, gateway_id: str, create_req: dict, existing: dict | None,
                   resync: bool = False) -> str:
    """Get-or-create-or-update one target and return its ID once READY.

    `resync=True` asks the Gateway to re-read an MCP server's tool list even when the target settings
    are unchanged (used when the support server's code was updated).
    """
    ac = ctx.client("bedrock-agentcore-control")
    name = create_req["name"]
    # Step 1: a FAILED target is deleted and recreated.
    if existing and existing["status"] == "FAILED":
        current = ac.get_gateway_target(gatewayIdentifier=gateway_id, targetId=existing["targetId"])
        info(f"target {name} is FAILED ({'; '.join(current.get('statusReasons', []))}); recreating")
        _delete_target(ac, gateway_id, existing["targetId"], name)
        existing = None

    # Step 2: missing target -> create it (AWS API CreateGatewayTarget) and wait for READY.
    if existing is None:
        target_id = call_with_iam_retry(ac.create_gateway_target, **create_req)["targetId"]
        info(f"{OK} create requested: target {name} ({target_id})")
        wait_target_ready(ac, gateway_id, target_id, f"target {name}")
        return target_id

    # Step 3: existing target -> let an unfinished change finish, then compare with the desired settings.
    target_id = existing["targetId"]
    if existing["status"] not in ("READY",) and existing["status"] not in TARGET_FAILED:
        wait_target_ready(ac, gateway_id, target_id, f"target {name} (in progress from an earlier run)")
    current = ac.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
    if target_differs(create_req, current) or current["status"] != "READY":
        # Step 4a: settings differ -> AWS API UpdateGatewayTarget.
        info(f"target {name} needs update")
        ac.update_gateway_target(**as_update_target_request(create_req, target_id))
        wait_target_ready(ac, gateway_id, target_id, f"target {name}")
    elif resync:
        # Step 4b: settings same, but the server's code changed -> AWS API SynchronizeGatewayTargets.
        # Same endpoint, new code: the Gateway still has the old tool list cached. Re-list it.
        info(f"target {name}: MCP runtime was updated, synchronizing its tool list")
        ac.synchronize_gateway_targets(**build_synchronize_request(gateway_id, [target_id]))
        wait_target_ready(ac, gateway_id, target_id, f"target {name} sync")
    else:
        # Step 4c: nothing to do.
        info(f"target {name} exists ({target_id}), up to date")
    return target_id


def deploy(ctx: Context) -> None:
    """Gateway, then the `orders` target, then the `support` target (serially)."""
    ac = ctx.client("bedrock-agentcore-control")
    # Step 1 + 2: the gateway itself, READY.
    gw = _ensure_gateway(ctx)
    gateway_id = gw["gatewayId"]
    existing = list_targets(ac, gateway_id)

    # Step 3: `orders` target = the Lambda from step 7 plus the tool list from tools_schema.json.
    orders_req = build_orders_target_request(gateway_id, ctx.state.section("lambda")["arn"], load_tool_schema())
    orders_id = _ensure_target(ctx, gateway_id, orders_req, existing.get(ORDERS_TARGET))
    ctx.state.put("gateway", targets={ORDERS_TARGET: orders_id})

    # Step 4: `support` target = the support MCP runtime's URL, with an OAuth token from AgentCore Identity.
    # The Gateway calls the MCP runtime's tools/list right away, so the runtime must be READY and the
    # credential provider usable (both earlier steps).
    support_req = build_support_target_request(
        gateway_id,
        rc.invocation_url(ctx.region, ctx.state.section("mcp_runtime")["arn"]),
        ctx.state.section("identity")["arn"],
        [ctx.names.scope],
    )
    # The flag is set by mcp_runtime.py (step 8) when the support server was created or updated this run.
    support_id = _ensure_target(ctx, gateway_id, support_req, existing.get(SUPPORT_TARGET),
                                resync=bool(ctx.flags.get("mcp_runtime_updated")))
    # Step 5: save both target IDs.
    ctx.state.put("gateway", targets={ORDERS_TARGET: orders_id, SUPPORT_TARGET: support_id})
    info(f"gateway URL: {gw['gatewayUrl']}")


# ------------------------------------------------------------------ destroy


def _delete_target(ac, gateway_id: str, target_id: str, name: str) -> None:
    """Delete one target (AWS API DeleteGatewayTarget) and wait until it is gone."""
    ignore_not_found(ac.delete_gateway_target, gatewayIdentifier=gateway_id, targetId=target_id)
    wait_for_status(f"target {name} deletion", lambda: _target_status(ac, gateway_id, target_id),
                    ready={"DELETED"}, failed={"FAILED"}, timeout=300, interval=3)


def _delete_gateway(ac, gateway_id: str) -> None:
    """Delete the gateway (AWS API DeleteGateway) and wait until it is gone."""
    ignore_not_found(ac.delete_gateway, gatewayIdentifier=gateway_id)
    wait_for_status(f"gateway {gateway_id} deletion", lambda: _gateway_status(ac, gateway_id),
                    ready={"DELETED"}, failed={"FAILED"}, timeout=600, interval=3)


def destroy_targets(ctx: Context) -> None:
    """A gateway with targets can't be deleted, so targets go first (after the Cedar policies)."""
    ac = ctx.client("bedrock-agentcore-control")
    gw = find_gateway(ac, ctx.names.gateway)
    if gw is None:
        info("gateway already gone (no targets to delete)")
        return
    targets = list_targets(ac, gw["gatewayId"])
    if not targets:
        info("no gateway targets")
    # One at a time, for the same reason they are created one at a time.
    for name, t in targets.items():
        _delete_target(ac, gw["gatewayId"], t["targetId"], name)
        info(f"{OK} deleted target {name}")
    ctx.state.section("gateway").pop("targets", None)
    ctx.state.save()


def destroy_gateway(ctx: Context) -> None:
    """Delete the gateway itself (its targets must already be gone)."""
    ac = ctx.client("bedrock-agentcore-control")
    gw = find_gateway(ac, ctx.names.gateway)
    if gw is None:
        info(f"gateway {ctx.names.gateway} already gone")
    else:
        _delete_gateway(ac, gw["gatewayId"])
        info(f"{OK} deleted gateway {gw['gatewayId']}")
    ctx.state.drop("gateway")


def destroy(ctx: Context) -> None:
    """Targets first, then the gateway. (destroy.py calls the two halves separately.)"""
    destroy_targets(ctx)
    destroy_gateway(ctx)
