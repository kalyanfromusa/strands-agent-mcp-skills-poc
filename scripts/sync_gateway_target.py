"""Ask the AgentCore Gateway to re-read the tool list of one or more targets, and wait until it's done.

    uv run python scripts/sync_gateway_target.py --region us-east-1 --gateway-id <id> --target support

What this file does
    After the support MCP server gets a new container image (a new version of its code), the Gateway
    still has the old tool list cached. This script calls SynchronizeGatewayTargets for the named
    targets, then polls each target until its status is READY again (or fails with the reason).

Where it fits
    Called by Terraform (infra/terraform/main.tf, terraform_data "sync_gateway_targets") with a
    local-exec provisioner, every time the support image tag changes. So the sync is part of
    `terraform apply`, and the pipeline needs no extra step. Credentials come from the environment
    Terraform runs in (the GitHub apply role, or AWS_PROFILE=personal locally).

Step by step
    1. List the gateway's targets and find the ids of the requested target names.
    2. Call SynchronizeGatewayTargets with those ids.
    3. Poll GetGatewayTarget every 5 seconds (up to --timeout seconds) until each one is READY.
    4. Exit 0 on success; exit 1 with the status reasons on failure or timeout.

Key terms
    Gateway target: one tool source behind the Gateway (orders = Lambda, support = MCP server).
    Synchronize: the Gateway asks the target for its tools/list again and updates its catalogue.
"""

from __future__ import annotations

import argparse
import sys
import time

import boto3

DONE = "READY"
FAILED = {"FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronize AgentCore Gateway targets and wait for READY.")
    parser.add_argument("--region", required=True)
    parser.add_argument("--gateway-id", required=True)
    parser.add_argument("--target", action="append", required=True, help="target name (repeatable), e.g. support")
    parser.add_argument("--timeout", type=int, default=300, help="seconds to wait for READY (default 300)")
    args = parser.parse_args()

    client = boto3.client("bedrock-agentcore-control", region_name=args.region)

    # Step 1: target name -> target id.
    ids: dict[str, str] = {}
    token = None
    while True:
        page = client.list_gateway_targets(gatewayIdentifier=args.gateway_id, **({"nextToken": token} if token else {}))
        for item in page.get("items", []):
            ids[item["name"]] = item["targetId"]
        token = page.get("nextToken")
        if not token:
            break
    missing = [t for t in args.target if t not in ids]
    if missing:
        print(f"gateway {args.gateway_id} has no target(s) named {', '.join(missing)}", file=sys.stderr)
        return 1
    wanted = [ids[t] for t in args.target]

    # Step 2: ask for the re-read.
    client.synchronize_gateway_targets(gatewayIdentifier=args.gateway_id, targetIdList=wanted)
    print(f"synchronizing {', '.join(args.target)} on {args.gateway_id} ...")

    # Step 3: wait for READY.
    deadline = time.monotonic() + args.timeout
    pending = dict(zip(wanted, args.target))
    while pending:
        for target_id, name in list(pending.items()):
            target = client.get_gateway_target(gatewayIdentifier=args.gateway_id, targetId=target_id)
            status = target.get("status", "")
            if status == DONE:
                print(f"  {name}: READY")
                del pending[target_id]
            elif status in FAILED:
                print(f"  {name}: {status} {target.get('statusReasons', [])}", file=sys.stderr)
                return 1
        if pending:
            if time.monotonic() > deadline:
                print(f"timed out waiting for {', '.join(pending.values())}", file=sys.stderr)
                return 1
            time.sleep(5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
