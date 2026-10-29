"""Create, set retention on, or delete one CloudWatch log group (used by Terraform).

What this file does
    Makes sure an AgentCore Runtime's log group exists and keeps logs for only N days, or deletes
    it on teardown.

Where it fits
    AgentCore creates the log group `/aws/bedrock-agentcore/runtimes/<runtime_id>-DEFAULT` by itself
    when a runtime starts. If Terraform also tried to create it, the two would race and apply could
    fail with "already exists". Terraform therefore runs this script after each runtime is created
    (modules/agent_runtime and modules/mcp_runtime) and again on `terraform destroy`.

Step by step
    ensure: 1. create the log group (fine if AgentCore already did)  2. set the retention policy.
    delete: 1. delete the log group (fine if it is already gone).

Key terms
    CloudWatch Logs: the AWS service that stores application log messages.
    Retention: how many days log messages are kept before AWS deletes them (fewer days = lower cost).
"""

from __future__ import annotations

import argparse

import boto3


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure or delete a CloudWatch log group.")
    parser.add_argument("action", choices=["ensure", "delete"])
    parser.add_argument("--name", required=True, help="log group name")
    parser.add_argument("--region", required=True)
    parser.add_argument("--days", type=int, default=7, help="retention in days (ensure only)")
    args = parser.parse_args()

    logs = boto3.client("logs", region_name=args.region)

    if args.action == "ensure":
        # Step 1: create the group. AgentCore may have created it already, which is fine.
        try:
            logs.create_log_group(logGroupName=args.name, tags={"project": "shopassist"})
            print(f"created {args.name}")
        except logs.exceptions.ResourceAlreadyExistsException:
            print(f"exists  {args.name}")
        # Step 2: keep log messages only for the configured number of days.
        logs.put_retention_policy(logGroupName=args.name, retentionInDays=args.days)
        print(f"retention {args.days} days")
        return 0

    # delete: remove the group on teardown. Already gone counts as success.
    try:
        logs.delete_log_group(logGroupName=args.name)
        print(f"deleted {args.name}")
    except logs.exceptions.ResourceNotFoundException:
        print(f"already gone {args.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
