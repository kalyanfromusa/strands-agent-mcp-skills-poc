"""Tear down everything `deploy.py` created, in reverse dependency order.

    uv run python infra/boto3/destroy.py [--region us-east-1] [--prefix shopassist] [--yes]

What this file does
    Deletes every AWS resource of one ShopAssist deployment, so nothing keeps costing money after a test.

Where it fits
    The counterpart of `deploy.py`. It calls the `destroy` functions of the modules in `steps/`, in the
    opposite order of the deploy, because AWS refuses to delete something that another resource still
    uses (for example, a gateway that still has targets).

Resources are found by name (with `state.json` as a shortcut), so destroy works even if the state file is
lost, and every step treats "already gone" as success, so it can be re-run after a partial failure. A
failing step doesn't stop the others; the failures are listed at the end and the exit code is 1.

Step by step (order and why)
  Cedar policies -> gateway targets -> gateway   (a gateway with targets can't be deleted)
  -> policy engine                               (only once no gateway references it)
  -> agent and MCP runtimes -> credential provider (its managed secret goes with it)
  -> Lambda -> DynamoDB tables -> Cognito domain, then pool (domain first, or the pool delete fails)
  -> leftover log groups -> IAM roles (inline policies first) -> S3 bucket (every object version first)
  -> budget -> local files

Key terms
    Log group: a CloudWatch folder where a service writes its log lines.
    Inline policy: a permission document stored inside one IAM role (it must be removed before the role).
    Object version: S3 keeps every old copy of a file in a versioned bucket; each copy must be deleted.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

# Make `common` and `steps` importable when the script is run from any folder.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (CROSS, DEFAULT_PREFIX, DEFAULT_REGION, OK, STATE_PATH, Context, Progress, State,  # noqa: E402
                    ignore_not_found, info, make_context)
from steps import (agent_runtime, budget, cognito, dynamodb, gateway, iam, identity, lambda_tools,  # noqa: E402
                   mcp_runtime, outputs, policy, s3)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Read the options. Region and prefix default to the ones the last deploy saved in state.json."""
    saved = State().data  # default to whatever the last deploy used
    p = argparse.ArgumentParser(description="Destroy the boto3-deployed ShopAssist stack.")
    p.add_argument("--region", default=saved.get("region", DEFAULT_REGION))
    p.add_argument("--prefix", default=saved.get("prefix", DEFAULT_PREFIX))
    p.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    return p.parse_args(argv)


def destroy_log_groups(ctx: Context) -> None:
    """Sweep log groups the services may have created after (or without) our own log-group calls.

    Lambda and AgentCore Runtime create their own log folders in CloudWatch the first time they write a
    log line. Those are not deleted with the service, so we find them by name prefix and delete them.
    """
    logs = ctx.client("logs")
    n = ctx.names
    prefixes = [f"/aws/lambda/{n.lambda_function}",
                f"/aws/bedrock-agentcore/runtimes/{n.agent_runtime}-",
                f"/aws/bedrock-agentcore/runtimes/{n.mcp_runtime}-"]
    count = 0
    for prefix in prefixes:
        # AWS API call: DescribeLogGroups lists the log groups whose names start with the prefix.
        for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix):
            for group in page["logGroups"]:
                # AWS API call: DeleteLogGroup removes the folder and every log line in it.
                ignore_not_found(logs.delete_log_group, logGroupName=group["logGroupName"])
                info(f"{OK} deleted log group {group['logGroupName']}")
                count += 1
    if not count:
        info("no leftover log groups")


def destroy_local_files(ctx: Context) -> None:
    """Remove `.deploy/outputs.json` (only if boto3 wrote it) and `state.json`, as the very last step."""
    outputs.destroy(ctx)
    if STATE_PATH.exists():
        STATE_PATH.unlink()
        info(f"removed {STATE_PATH.name}")


def steps():
    """(title, function(ctx)) in destroy order: the reverse of the deploy order (see the module docstring)."""
    return [
        ("Cedar policies", policy.destroy_policies),
        ("Gateway targets", gateway.destroy_targets),
        ("Gateway", gateway.destroy_gateway),
        ("Policy engine", policy.destroy_engine),
        ("Agent runtime", agent_runtime.destroy),
        ("Support MCP runtime", mcp_runtime.destroy),
        ("OAuth2 credential provider", identity.destroy),
        ("Lambda orders tools", lambda_tools.destroy),
        ("DynamoDB tables", dynamodb.destroy),
        ("Cognito domain and user pool", cognito.destroy),
        ("Log groups", destroy_log_groups),
        ("IAM roles", iam.destroy),
        ("S3 bucket (all object versions)", s3.destroy),
        ("Budget", budget.destroy),
    ]


def main(argv: list[str] | None = None) -> int:
    """Ask for confirmation, run every destroy step, and report any failures at the end."""
    # Step 1: connect to AWS and ask the user to type the prefix, so nothing is deleted by accident.
    args = parse_args(argv)
    ctx = make_context(args.region, args.prefix)
    print(f"About to DELETE every '{args.prefix}' resource in account {ctx.account_id}, region {args.region}.")
    if not args.yes and input("Type the prefix to confirm: ").strip() != args.prefix:
        print("Aborted.")
        return 1

    # Step 2: run every destroy step. Unlike deploy, a failing step does not stop the run: we try to
    # delete as much as possible (to stop costs) and list what failed at the end.
    plan = steps()
    progress = Progress(len(plan) + 1)
    failures: list[str] = []
    for title, fn in plan:
        try:
            with progress.step(title):
                fn(ctx)
        except Exception as err:  # noqa: BLE001 - keep going, report at the end
            traceback.print_exc(limit=1)
            failures.append(f"{title}: {err}")
    # Step 3: if anything failed, keep state.json so the next run can find what is left.
    if failures:
        print(f"\n{CROSS} Destroy finished with {len(failures)} failed step(s) after {progress.elapsed()}:")
        for f in failures:
            print(f"  - {f}")
        print("State kept. Re-run destroy.py once the cause is fixed.")
        return 1
    # Step 4: everything in AWS is gone, so remove the local files too.
    with progress.step("Local files (.deploy/outputs.json, state.json)"):
        destroy_local_files(ctx)
    print(f"\n{OK} Destroyed in {progress.elapsed()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
