"""Roll a new container image out to an existing AgentCore Runtime, and re-sync a Gateway MCP target.

What this file does
    The CI/CD pipeline's only "deploy" action (docs/10-ci-cd.md). Terraform (or infra/boto3) creates the
    runtimes once; after that, every push to main only needs to swap the image. This script:
      update-runtime  reads the runtime (GetAgentRuntime), sends UpdateAgentRuntime with every setting
                      copied unchanged (role, network, protocol, authorizer, request headers, env vars,
                      lifecycle, ...) and only `agentRuntimeArtifact` replaced by
                      `containerConfiguration.containerUri`, then waits until the new version is READY
                      and the DEFAULT endpoint serves it.
      sync-target     calls SynchronizeGatewayTargets for one Gateway target (by name, e.g. `support`)
                      so the Gateway re-reads the MCP server's tool list, then waits until it is READY.

Where it fits
    .github/workflows/deploy.yml, job `deploy`, after the images are pushed to ECR. It never runs
    Terraform. In container mode the main Terraform stack ignores changes to the container URI
    (docs/ci-cd-integration-patch.md), so the two don't fight.

Step by step (update-runtime)
    1. GetAgentRuntime. Refuse if it is not in container mode (unless --allow-mode-switch), because a
       code->container switch would be reverted by the next `terraform apply` of a zip-mode stack.
    2. If it already runs that exact image, stop (nothing to do) unless --force.
    3. Build the UpdateAgentRuntime request (pure function, unit-tested against the botocore model).
    4. Send it; AgentCore creates a new runtime version.
    5. Poll until that version is READY, then until the DEFAULT endpoint's liveVersion is that version.

Usage
    python scripts/ci/update_runtime_image.py update-runtime --runtime-id shopassist_agent-AbC123 \\
        --image 123456789012.dkr.ecr.us-east-1.amazonaws.com/shopassist-agent:<sha> [--dry-run]
    python scripts/ci/update_runtime_image.py sync-target --gateway-id shopassist-gateway-abc --target-name support

Key terms
    Runtime version: every UpdateAgentRuntime creates version N+1; the DEFAULT endpoint follows the latest.
    containerUri: <account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag> (an arm64 image in ECR).
    Gateway target sync: an MCP target's tool list is cached by the Gateway; syncing re-lists it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Callable

# Every UpdateAgentRuntime input that GetAgentRuntime also returns, except the id and the artifact.
# tests/test_ci_update_runtime.py checks this tuple against the installed botocore model, so a field
# AWS adds later can't be silently dropped (an Update without it would reset that setting).
PRESERVED_FIELDS: tuple[str, ...] = (
    "roleArn",
    "networkConfiguration",
    "description",
    "authorizerConfiguration",
    "requestHeaderConfiguration",
    "protocolConfiguration",
    "lifecycleConfiguration",
    "metadataConfiguration",
    "environmentVariables",
    "filesystemConfigurations",
    "capacityProviderConfiguration",
    "platformVersion",
)

RUNTIME_FAILED = {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED", "DELETING"}
TARGET_FAILED = {"FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL"}
TARGET_PENDING_AUTH = {"CREATE_PENDING_AUTH", "UPDATE_PENDING_AUTH", "SYNCHRONIZE_PENDING_AUTH"}


# ------------------------------------------------------------------ request building (pure)


def current_image(runtime: dict) -> str | None:
    """The container URI the runtime runs now, or None in code (zip) mode."""
    return ((runtime.get("agentRuntimeArtifact") or {}).get("containerConfiguration") or {}).get("containerUri")


def artifact_mode(runtime: dict) -> str:
    """'container', 'code' or 'unknown', from GetAgentRuntime's agentRuntimeArtifact."""
    art = runtime.get("agentRuntimeArtifact") or {}
    if "containerConfiguration" in art:
        return "container"
    if "codeConfiguration" in art:
        return "code"
    return "unknown"


def build_update_request(runtime: dict, image_uri: str) -> dict:
    """UpdateAgentRuntime request = the current settings + the new image, nothing else changed.

    `runtime` is a GetAgentRuntime response. UpdateAgentRuntime is a full replace (not a patch): any
    optional field left out is reset, which is why every PRESERVED_FIELDS entry is copied when present.
    """
    last = image_uri.rsplit("/", 1)[-1] if image_uri else ""
    if "/" not in (image_uri or "") or (":" not in last and "@sha256:" not in image_uri):
        raise ValueError(f"image URI must include a tag or digest: {image_uri!r}")
    req: dict[str, Any] = {
        "agentRuntimeId": runtime["agentRuntimeId"],
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image_uri}},
    }
    for key in PRESERVED_FIELDS:
        value = runtime.get(key)
        if value is not None:
            req[key] = value
    return req


def find_target_id(targets: list[dict], name: str) -> str:
    """Target id for a target name from ListGatewayTargets items."""
    for t in targets:
        if t.get("name") == name:
            return t["targetId"]
    raise SystemExit(f"gateway target {name!r} not found (have: {', '.join(t.get('name', '?') for t in targets)})")


def build_sync_request(gateway_id: str, target_id: str) -> dict:
    """SynchronizeGatewayTargets request for a single target."""
    return {"gatewayIdentifier": gateway_id, "targetIdList": [target_id]}


# ------------------------------------------------------------------ AWS helpers


def log(msg: str) -> None:
    print(msg, flush=True)


def poll(what: str, fn: Callable[[], tuple[bool, str]], timeout: float, interval: float = 10) -> None:
    """Call fn() -> (done, status_text) until done; raise on timeout. fn raises on failure."""
    t0, last = time.monotonic(), ""
    while True:
        done, status = fn()
        if status != last:
            log(f"  {what}: {status} ({time.monotonic() - t0:.0f}s)")
            last = status
        if done:
            return
        if time.monotonic() - t0 > timeout:
            raise SystemExit(f"timed out after {timeout:.0f}s waiting for {what} (last status {status})")
        time.sleep(interval)


def wait_runtime_version(ac, runtime_id: str, version: str, timeout: float) -> None:
    """Wait for runtime `version` to be READY, then for the DEFAULT endpoint to serve it."""
    def runtime_ready() -> tuple[bool, str]:
        rt = ac.get_agent_runtime(agentRuntimeId=runtime_id)
        status, cur = rt.get("status"), rt.get("agentRuntimeVersion")
        if status in RUNTIME_FAILED:
            raise SystemExit(f"runtime {runtime_id} {status}: {rt.get('failureReason', '')}")
        return status == "READY" and cur == version, f"{status} (version {cur}, want {version})"

    def endpoint_live() -> tuple[bool, str]:
        ep = ac.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName="DEFAULT")
        status, live = ep.get("status"), ep.get("liveVersion")
        if status in RUNTIME_FAILED:
            raise SystemExit(f"endpoint DEFAULT {status}: {ep.get('failureReason', '')}")
        return status == "READY" and live == version, f"{status} (liveVersion {live})"

    poll(f"runtime {runtime_id}", runtime_ready, timeout)
    poll(f"runtime {runtime_id} endpoint DEFAULT", endpoint_live, timeout)


def cmd_update_runtime(args: argparse.Namespace, ac) -> int:
    # Step 1: read the runtime as it is now.
    rt = ac.get_agent_runtime(agentRuntimeId=args.runtime_id)
    mode, before = artifact_mode(rt), current_image(rt)
    log(f"runtime {rt.get('agentRuntimeName')} ({args.runtime_id}) version {rt.get('agentRuntimeVersion')} "
        f"status {rt.get('status')} mode {mode} image {before}")
    if rt.get("status") in ("CREATING", "UPDATING"):
        log("  a change is still in progress; waiting for it first")
        wait_runtime_version(ac, args.runtime_id, rt["agentRuntimeVersion"], args.timeout)
        rt = ac.get_agent_runtime(agentRuntimeId=args.runtime_id)
    if mode != "container" and not args.allow_mode_switch:
        raise SystemExit(f"runtime is in {mode} mode; switch the main stack to artifact_type = \"container\" "
                         "first (docs/10-ci-cd.md), or pass --allow-mode-switch")
    # Step 2: nothing to do?
    if before == args.image and not args.force:
        log("  already running this image; nothing to do (use --force to create a new version anyway)")
        return 0
    # Step 3: the request.
    req = build_update_request(rt, args.image)
    if args.dry_run:
        log(json.dumps(req, indent=2, default=str))
        return 0
    # Step 4 + 5: update and wait.
    resp = ac.update_agent_runtime(**req)
    version = resp["agentRuntimeVersion"]
    log(f"  update requested: new version {version}")
    if args.no_wait:
        return 0
    wait_runtime_version(ac, args.runtime_id, version, args.timeout)
    log(f"OK runtime {args.runtime_id} version {version} READY with {args.image}")
    return 0


def cmd_sync_target(args: argparse.Namespace, ac) -> int:
    target_id = args.target_id
    if not target_id:
        items, token = [], None
        while True:
            page = ac.list_gateway_targets(gatewayIdentifier=args.gateway_id, **({"nextToken": token} if token else {}))
            items += page.get("items", [])
            token = page.get("nextToken")
            if not token:
                break
        target_id = find_target_id(items, args.target_name)
    req = build_sync_request(args.gateway_id, target_id)
    if args.dry_run:
        log(json.dumps(req, indent=2))
        return 0
    ac.synchronize_gateway_targets(**req)
    log(f"sync requested for target {args.target_name} ({target_id})")

    def ready() -> tuple[bool, str]:
        t = ac.get_gateway_target(gatewayIdentifier=args.gateway_id, targetId=target_id)
        status = t.get("status")
        if status in TARGET_FAILED or status in TARGET_PENDING_AUTH:
            raise SystemExit(f"target {target_id} {status}: {t.get('statusReasons')}")
        return status == "READY", str(status)

    poll(f"target {args.target_name}", ready, args.timeout, interval=5)
    log(f"OK target {args.target_name} synchronized")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    # Shared options live on each subcommand, so they go after it: `update-runtime --dry-run ...`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--region", help="AWS region (default: from the environment)")
    common.add_argument("--timeout", type=float, default=900, help="seconds to wait per phase (default 900)")
    common.add_argument("--dry-run", action="store_true", help="print the request instead of sending it")

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)

    u = sub.add_parser("update-runtime", parents=[common], help="swap a runtime's container image, keep everything else")
    u.add_argument("--runtime-id", required=True, help="agentRuntimeId, e.g. shopassist_agent-AbC123xyz")
    u.add_argument("--image", required=True, help="full image URI with tag or digest")
    u.add_argument("--allow-mode-switch", action="store_true", help="allow switching a code (zip) runtime to container")
    u.add_argument("--force", action="store_true", help="update even if the runtime already runs this image")
    u.add_argument("--no-wait", action="store_true", help="don't wait for READY")

    s = sub.add_parser("sync-target", parents=[common], help="SynchronizeGatewayTargets for one MCP target")
    s.add_argument("--gateway-id", required=True)
    s.add_argument("--target-name", default="support")
    s.add_argument("--target-id", help="skip the lookup by name")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    import boto3  # imported here so the pure functions are testable without creating clients

    ac = boto3.client("bedrock-agentcore-control", region_name=args.region)
    if args.command == "update-runtime":
        return cmd_update_runtime(args, ac)
    return cmd_sync_target(args, ac)


if __name__ == "__main__":
    sys.exit(main())
