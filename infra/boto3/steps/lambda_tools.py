"""Lambda `<prefix>-orders-tools`: the `orders` MCP tools behind the Gateway.

What this file does
    Deploy step 7. Creates (or updates) the Lambda function that runs the orders tools: look up an order,
    list a customer's orders, check and create returns.

Where it fits
    Needs the code zip in S3 (step 1), its IAM role (step 5) and the tables (step 6). The Gateway's
    `orders` target (step 9) points at this function, and the Gateway calls it whenever the agent uses an
    `orders___...` tool.

The Gateway turns MCP `tools/call orders___get_order {...}` into a Lambda invocation whose event is the
tool arguments, and whose `context.client_context.custom["bedrockAgentCoreToolName"]` is the prefixed
tool name. The Lambda has no MCP code at all; the tool *schemas* live on the Gateway target
(`gateway.py`, inline schema from `mcp_servers/orders_lambda/tools_schema.json`).

python3.12 on arm64 (cheaper), code from the versioned S3 object uploaded by `s3.py`. The log group is
created up front so it gets the 7-day retention instead of "never expire".

Step by step
    1. Create the log group and set its retention to 7 days.
    2. If the function is missing, create it from the S3 zip and wait until it is Active.
    3. If it exists: upload the new code only if the zip's fingerprint changed, then update the settings
       (role, environment, memory, timeout...) only if they differ. Wait after each change.
    4. Save the function name and ARN in state.json.

Key terms
    Lambda: a small function AWS runs on demand; you pay only while it runs.
    Handler: the Python function Lambda calls, here `lambda_handler` in handler.py.
    arm64: the Graviton processor type, cheaper than x86 for the same work.
    SHA-256 fingerprint: a short code computed from a file; it changes whenever the file changes.
"""

from __future__ import annotations

import base64
import hashlib

from common import (ARTIFACTS, LOG_RETENTION_DAYS, Context, OK, call_with_iam_retry, ignore_not_found, info,
                    is_not_found)

# Function settings: Python version, entry point, max run time (seconds) and memory (MB).
RUNTIME = "python3.12"
HANDLER = "handler.lambda_handler"
TIMEOUT = 30
MEMORY = 256


def build_environment(ctx: Context) -> dict[str, str]:
    """Settings the tool code reads: use DynamoDB (not the in-memory test store) and which tables."""
    n = ctx.names
    # AWS_REGION is reserved (set by Lambda itself), so it is not passed here.
    return {"STORE": "dynamodb", "ORDERS_TABLE": n.orders_table, "RETURNS_TABLE": n.returns_table}


def build_create_log_group_request(name: str, tags: dict[str, str]) -> dict:
    """CreateLogGroup request: the CloudWatch folder the function writes its logs to."""
    return {"logGroupName": name, "tags": tags}


def build_retention_request(name: str, days: int = LOG_RETENTION_DAYS) -> dict:
    """PutRetentionPolicy request: delete log lines older than `days` (keeps storage costs low)."""
    return {"logGroupName": name, "retentionInDays": days}


def build_create_function_request(name: str, role_arn: str, bucket: str, key: str, version_id: str | None,
                                  env: dict[str, str], tags: dict[str, str]) -> dict:
    """CreateFunction request.

    Code: the zip in S3, pinned to one exact file version (S3ObjectVersion) so we know which code runs.
    Role: the IAM role the function runs as (access to the orders and returns tables).
    LoggingConfig: write to the log group created in step 1.
    """
    code = {"S3Bucket": bucket, "S3Key": key}
    if version_id:
        code["S3ObjectVersion"] = version_id
    return {
        "FunctionName": name,
        "Description": "ShopAssist orders MCP tools (AgentCore Gateway Lambda target)",
        "Runtime": RUNTIME,
        "Architectures": ["arm64"],
        "Handler": HANDLER,
        "Role": role_arn,
        "Code": code,
        "Timeout": TIMEOUT,
        "MemorySize": MEMORY,
        "Environment": {"Variables": env},
        "LoggingConfig": {"LogFormat": "Text", "LogGroup": f"/aws/lambda/{name}"},
        "Tags": tags,
    }


def build_update_configuration_request(name: str, role_arn: str, env: dict[str, str]) -> dict:
    """UpdateFunctionConfiguration request: re-apply the settings (not the code) of an existing function."""
    return {"FunctionName": name, "Role": role_arn, "Handler": HANDLER, "Runtime": RUNTIME,
            "Timeout": TIMEOUT, "MemorySize": MEMORY, "Environment": {"Variables": env}}


def sha256_b64(path) -> str:
    """Fingerprint of the local zip, in the same format Lambda reports (`CodeSha256`), to spot code changes."""
    return base64.b64encode(hashlib.sha256(path.read_bytes()).digest()).decode()


def ensure_log_group(ctx: Context, name: str) -> None:
    """Create the log group if needed ("already exists" is fine) and always set the 7-day retention."""
    logs = ctx.client("logs")
    try:
        logs.create_log_group(**build_create_log_group_request(name, ctx.names.tags))
        info(f"{OK} created log group {name}")
    except Exception as err:  # noqa: BLE001
        if "ResourceAlreadyExistsException" not in str(err):
            raise
    logs.put_retention_policy(**build_retention_request(name))


def deploy(ctx: Context) -> None:
    """Create the orders Lambda, or roll out code and setting changes to it."""
    lam = ctx.client("lambda")
    n = ctx.names
    name = n.lambda_function
    # Inputs from earlier steps: the role (step 5) and the uploaded zip (step 1).
    role_arn = ctx.state.section("iam")["roles"]["lambda"]["arn"]
    art = ctx.state.section("s3")["artifacts"]["orders_lambda"]
    bucket = ctx.state.section("s3")["bucket"]
    env = build_environment(ctx)

    # Step 1: log group with retention, before the function first writes to it.
    ensure_log_group(ctx, f"/aws/lambda/{name}")

    # AWS API GetFunction: does the function exist already? "Not found" means no.
    try:
        current = lam.get_function(FunctionName=name)["Configuration"]
    except Exception as err:  # noqa: BLE001
        if not is_not_found(err):
            raise
        current = None

    if current is None:
        # Step 2: AWS API CreateFunction. Retried while the brand-new IAM role is not usable yet.
        # Then wait until the function is Active (ready to be called).
        req = build_create_function_request(name, role_arn, bucket, art["key"], art["version_id"], env, n.tags)
        arn = call_with_iam_retry(lam.create_function, **req)["FunctionArn"]
        info(f"{OK} created function {name}")
        lam.get_waiter("function_active_v2").wait(FunctionName=name)
    else:
        arn = current["FunctionArn"]
        # Step 3a: code. Compare the fingerprint of the local zip with what Lambda is running; if they
        # differ, AWS API UpdateFunctionCode points the function at the new zip version in S3.
        if current["CodeSha256"] != sha256_b64(ARTIFACTS["orders_lambda"]):
            code = {"S3Bucket": bucket, "S3Key": art["key"]}
            if art["version_id"]:
                code["S3ObjectVersion"] = art["version_id"]
            lam.update_function_code(FunctionName=name, **code)
            # Lambda refuses another change while one is in progress, so wait for this one to finish.
            lam.get_waiter("function_updated_v2").wait(FunctionName=name)
            info(f"{OK} updated code of {name} (S3 version {art['version_id']})")
        else:
            info(f"function {name} code unchanged")
        # Step 3b: settings. Update (AWS API UpdateFunctionConfiguration) only if one of them differs.
        drift = (current["Role"] != role_arn
                 or current.get("Environment", {}).get("Variables", {}) != env
                 or current["Handler"] != HANDLER or current["Runtime"] != RUNTIME
                 or current["Timeout"] != TIMEOUT or current["MemorySize"] != MEMORY)
        if drift:
            call_with_iam_retry(lam.update_function_configuration,
                                **build_update_configuration_request(name, role_arn, env))
            lam.get_waiter("function_updated_v2").wait(FunctionName=name)
            info(f"{OK} updated configuration of {name}")
    # Step 4: remember the function; the Gateway step needs its ARN.
    ctx.state.put("lambda", name=name, arn=arn)


def destroy(ctx: Context) -> None:
    """Delete the function (AWS API DeleteFunction) and its log group (DeleteLogGroup)."""
    name = ctx.names.lambda_function
    if ignore_not_found(ctx.client("lambda").delete_function, FunctionName=name) is None:
        info(f"function {name} already gone")
    else:
        info(f"{OK} deleted function {name}")
    ignore_not_found(ctx.client("logs").delete_log_group, logGroupName=f"/aws/lambda/{name}")
    info(f"{OK} deleted log group /aws/lambda/{name}")
    ctx.state.drop("lambda")
