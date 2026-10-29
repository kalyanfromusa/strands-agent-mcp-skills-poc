"""Shared AgentCore Runtime plumbing used by `mcp_runtime.py` and `agent_runtime.py`.

What this file does
    The create / update / wait / delete logic for AgentCore Runtime, written once and used for both
    hosted programs: the support MCP server (deploy step 8) and the agent (deploy step 11).

Where it fits
    `mcp_runtime.py` and `agent_runtime.py` build their own create request, then call `ensure_runtime()`
    here. `gateway.py` uses `invocation_url()` to point its `support` target at the MCP runtime.

An AgentCore Runtime runs your code in per-session microVMs. With **direct code deployment** the
artifact is a zip in S3 plus a managed Python runtime and an entry point, so no Docker/ECR is needed:

    agentRuntimeArtifact.codeConfiguration = {
        "code": {"s3": {"bucket", "prefix" (= the object key), "versionId"}},
        "runtime": "PYTHON_3_12",
        "entryPoint": ["app.py"],
    }

Pinning `versionId` is what turns "I rebuilt the zip" into an UpdateAgentRuntime (new runtime version).
Every runtime automatically gets a `DEFAULT` endpoint that always points at the latest version.
There is no botocore waiter for runtimes, so we poll GetAgentRuntime until READY.

Step by step (`ensure_runtime`)
    1. Look the runtime up by name. If it is CREATE_FAILED, delete it so it can be created fresh.
    2. If it is missing, create it and wait until READY.
    3. If it exists, let any unfinished change finish, then compare its settings with the desired ones.
    4. If anything differs, update it (this makes a new version) and wait until that version is READY.

Key terms
    AgentCore Runtime: AWS's managed hosting for agent and tool code; it starts and stops it on demand.
    microVM: a tiny, isolated virtual machine; each user session gets its own.
    Docker/ECR: the usual container packaging and image store, not needed with zip deployment.
    Version: every update of a runtime gets a new number (1, 2, 3...); the DEFAULT endpoint follows the latest.
"""

from __future__ import annotations

from urllib.parse import quote

from common import (LOG_RETENTION_DAYS, Context, OK, call_with_iam_retry, ignore_not_found, info, is_not_found,
                    paginate, wait_for_status)

PYTHON_RUNTIME = "PYTHON_3_12"
# Statuses that mean "went wrong", so waiting stops with an error.
FAILED = {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}


# ------------------------------------------------------------------ request builders (pure)


def build_code_artifact(bucket: str, key: str, version_id: str | None, entry_point: str) -> dict:
    """The `agentRuntimeArtifact` field: which zip in S3 (exact version), which Python, which file to start."""
    s3 = {"bucket": bucket, "prefix": key}  # "prefix" is the full object key of the zip
    if version_id:
        s3["versionId"] = version_id
    return {"codeConfiguration": {"code": {"s3": s3}, "runtime": PYTHON_RUNTIME, "entryPoint": [entry_point]}}


def build_jwt_authorizer(discovery_url: str, allowed_clients: list[str],
                         custom_claims: list[dict] | None = None) -> dict:
    """The `customJWTAuthorizer` login check, used by both runtimes and by the Gateway.

    - discoveryUrl: where the service reads Cognito's public keys to verify that a token is genuine.
    - allowedClients: only tokens issued to these Cognito app clients are accepted.
    - customClaims (optional): extra rules on token fields, e.g. "groups must contain shop-admin or
      shop-viewer" (used by the agent runtime only).
    """
    jwt: dict = {"discoveryUrl": discovery_url, "allowedClients": allowed_clients}
    if custom_claims:
        jwt["customClaims"] = custom_claims
    return {"customJWTAuthorizer": jwt}


def as_update_request(create_request: dict, runtime_id: str) -> dict:
    """UpdateAgentRuntime takes the full desired spec again, minus the name and tags."""
    req = {k: v for k, v in create_request.items() if k not in ("agentRuntimeName", "tags")}
    req["agentRuntimeId"] = runtime_id
    return req


def invocation_url(region: str, runtime_arn: str, qualifier: str = "DEFAULT") -> str:
    """HTTPS data-plane URL of a runtime endpoint (used for the Gateway `support` target and JWT callers).

    The runtime's ARN is put inside the web address, so it is URL-encoded (":" and "/" become %3A, %2F).
    """
    return (f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{quote(runtime_arn, safe='')}"
            f"/invocations?qualifier={qualifier}")


# ------------------------------------------------------------------ helpers


def find_runtime(ac, name: str) -> dict | None:
    """Find a runtime by name (AWS API ListAgentRuntimes, all pages). None if it does not exist."""
    for rt in paginate(ac.list_agent_runtimes, "agentRuntimes"):
        if rt["agentRuntimeName"] == name:
            return rt
    return None


def _get(ac, runtime_id: str) -> dict | None:
    """Full details of a runtime (AWS API GetAgentRuntime), or None if it does not exist."""
    try:
        return ac.get_agent_runtime(agentRuntimeId=runtime_id)
    except Exception as err:  # noqa: BLE001
        if is_not_found(err):
            return None
        raise


def wait_ready(ac, runtime_id: str, what: str, expected_version: str | None = None, timeout: float = 900) -> dict:
    """Poll until the runtime is READY (up to 15 minutes) and return its details.

    With `expected_version`, READY only counts once the new version is the one reported, so we don't
    mistake the old version's READY for the end of an update.
    """
    def status():
        rt = _get(ac, runtime_id) or {}
        if expected_version and rt.get("status") == "READY" and rt.get("agentRuntimeVersion") != expected_version:
            return "UPDATE_PENDING", ""  # right after UpdateAgentRuntime the old version may still say READY
        return rt.get("status"), rt.get("failureReason", "")

    wait_for_status(what, status, ready={"READY"}, failed=FAILED | {"DELETED"}, timeout=timeout)
    return _get(ac, runtime_id)


def _differs(desired: dict, current: dict) -> list[str]:
    """Names of the top-level fields whose desired value isn't what the runtime currently has."""
    keys = ["agentRuntimeArtifact", "roleArn", "networkConfiguration", "protocolConfiguration",
            "environmentVariables", "authorizerConfiguration", "requestHeaderConfiguration", "description"]
    return [k for k in keys if desired.get(k) != current.get(k)]


def ensure_runtime(ctx: Context, create_request: dict) -> tuple[dict, bool]:
    """Get-or-create-or-update a runtime. Returns (GetAgentRuntime response once READY, changed?)."""
    ac = ctx.client("bedrock-agentcore-control")
    name = create_request["agentRuntimeName"]
    summary = find_runtime(ac, name)

    # Step 1: a runtime that failed to create can't be updated; delete it and start over.
    if summary and summary["status"] == "CREATE_FAILED":
        current = _get(ac, summary["agentRuntimeId"]) or {}
        info(f"runtime {name} is CREATE_FAILED ({current.get('failureReason')}); deleting and recreating")
        delete_runtime(ctx, name)
        summary = None

    # Step 2: missing -> AWS API CreateAgentRuntime (retried while a new IAM role is not usable yet).
    if summary is None:
        resp = call_with_iam_retry(ac.create_agent_runtime, **create_request)
        info(f"{OK} create requested: {resp['agentRuntimeId']} (version {resp['agentRuntimeVersion']})")
        return wait_ready(ac, resp["agentRuntimeId"], f"runtime {name}"), True

    # Step 3: exists -> wait for any change still running from an earlier run, then compare settings.
    runtime_id = summary["agentRuntimeId"]
    if summary["status"] in ("CREATING", "UPDATING"):
        wait_ready(ac, runtime_id, f"runtime {name} (in progress from an earlier run)")
    current = _get(ac, runtime_id)
    changed = _differs(create_request, current)
    if not changed and current["status"] == "READY":
        info(f"runtime {name} exists ({runtime_id}, version {current['agentRuntimeVersion']}), up to date")
        return current, False

    # Step 4: something differs (new zip version, new settings) -> AWS API UpdateAgentRuntime, which
    # creates a new version; wait until that exact version is READY.
    info(f"runtime {name} needs update: {', '.join(changed) or 'status ' + current['status']}")
    resp = call_with_iam_retry(ac.update_agent_runtime, **as_update_request(create_request, runtime_id))
    info(f"{OK} update requested (new version {resp['agentRuntimeVersion']})")
    return wait_ready(ac, runtime_id, f"runtime {name}", expected_version=resp["agentRuntimeVersion"]), True


def ensure_log_retention(ctx: Context, runtime_id: str) -> None:
    """The runtime writes to /aws/bedrock-agentcore/runtimes/<id>-DEFAULT. Create it now to set retention."""
    logs = ctx.client("logs")
    group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
    # AWS API CreateLogGroup ("already exists" is fine), then PutRetentionPolicy (keep logs 7 days).
    try:
        logs.create_log_group(logGroupName=group, tags=ctx.names.tags)
    except Exception as err:  # noqa: BLE001
        if "ResourceAlreadyExistsException" not in str(err):
            raise
    logs.put_retention_policy(logGroupName=group, retentionInDays=LOG_RETENTION_DAYS)
    info(f"log group {group}: retention {LOG_RETENTION_DAYS} days")


def delete_runtime(ctx: Context, name: str) -> None:
    """Delete a runtime and all of its log groups (fine if it is already gone)."""
    ac = ctx.client("bedrock-agentcore-control")
    summary = find_runtime(ac, name)
    if summary is None:
        info(f"runtime {name} already gone")
    else:
        runtime_id = summary["agentRuntimeId"]
        # Step 1: a runtime in the middle of a change can't be deleted, so let that change finish first.
        if summary["status"] in ("CREATING", "UPDATING"):
            wait_for_status(f"runtime {name}", lambda: ((_get(ac, runtime_id) or {}).get("status"), ""),
                            ready={"READY", "DELETED"} | FAILED, timeout=900)
        # Step 2: AWS API DeleteAgentRuntime, then wait until it is gone.
        ignore_not_found(ac.delete_agent_runtime, agentRuntimeId=runtime_id)
        wait_for_status(f"runtime {name} deletion", lambda: ((_get(ac, runtime_id) or {}).get("status"), ""),
                        ready={"DELETED"}, failed={"DELETE_FAILED"}, timeout=600)

    # Step 3: remove the log groups, which AWS does not delete with the runtime.
    # Log groups are named after the runtime ID ("<name>-<suffix>-DEFAULT"); delete them all by prefix.
    logs = ctx.client("logs")
    prefix = f"/aws/bedrock-agentcore/runtimes/{name}-"
    for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix):
        for group in page["logGroups"]:
            ignore_not_found(logs.delete_log_group, logGroupName=group["logGroupName"])
            info(f"{OK} deleted log group {group['logGroupName']}")
