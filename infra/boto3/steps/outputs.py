"""`.deploy/outputs.json`: the contract file every client script reads (design.md sections 6 and 8).

What this file does
    Deploy step 13 (the last one). Writes a small JSON file listing the addresses and IDs that the client
    scripts need to reach the deployed system. No AWS calls are made here.

Where it fits
    It reads what earlier steps saved in state.json. `scripts/invoke_agent.py`, `scripts/demo_access.py`
    and others read the file it writes.

Terraform writes exactly the same keys (`deployed_by: "terraform"`), so `scripts/invoke_agent.py` and
`scripts/demo_access.py` don't care which path deployed the stack. The demo user passwords are not in
here; `cognito.py` writes them to `.deploy/users.json`.

Step by step
    1. Map the state.json values onto the agreed key names.
    2. Write `.deploy/outputs.json`.
    (On destroy: delete the file, but only if this boto3 path wrote it.)

Key terms
    Contract file: a file whose layout is agreed between two parts of the system, so either side can change
    internally without breaking the other.
"""

from __future__ import annotations

import json

from common import DEPLOY_DIR, Context, OK, info

OUTPUTS_FILE = DEPLOY_DIR / "outputs.json"

# The agreed key names. A test checks that Terraform outputs exactly the same keys.
OUTPUT_KEYS = (  # design.md section 6, then the section 8 additions
    "region", "account_id", "artifacts_bucket", "skills_s3_uri", "agent_runtime_arn", "support_mcp_runtime_arn",
    "gateway_id", "gateway_url", "cognito_user_pool_id", "cognito_client_id", "cognito_token_url",
    "oauth_provider_name", "deployed_by", "cognito_users_client_id", "policy_engine_id",
)


def build_outputs(state: dict) -> dict:
    """Map `state.json` onto the contract keys. Pure, so it is unit-tested."""
    r = state["resources"]
    return {
        "region": state["region"],
        "account_id": state["account_id"],
        "artifacts_bucket": r["s3"]["bucket"],
        "skills_s3_uri": r["s3"]["skills_s3_uri"],
        "agent_runtime_arn": r["agent_runtime"]["arn"],
        "support_mcp_runtime_arn": r["mcp_runtime"]["arn"],
        "gateway_id": r["gateway"]["id"],
        "gateway_url": r["gateway"]["url"],
        "cognito_user_pool_id": r["cognito"]["user_pool_id"],
        # "cognito_client_id" is the machine (M2M) client; the people client has its own key below.
        "cognito_client_id": r["cognito"]["m2m_client_id"],
        "cognito_token_url": r["cognito"]["token_url"],
        "oauth_provider_name": r["identity"]["name"],
        "deployed_by": "boto3",
        "cognito_users_client_id": r["cognito"]["users_client_id"],
        "policy_engine_id": r["policy"]["engine_id"],
    }


def deploy(ctx: Context) -> dict:
    """Write `.deploy/outputs.json` from the current state and return its contents."""
    outputs = build_outputs(ctx.state.data)
    DEPLOY_DIR.mkdir(exist_ok=True)
    OUTPUTS_FILE.write_text(json.dumps(outputs, indent=2) + "\n", encoding="utf-8")
    info(f"{OK} wrote {OUTPUTS_FILE.relative_to(DEPLOY_DIR.parent).as_posix()}")
    return outputs


def destroy(ctx: Context) -> None:
    """Only remove the file if this path wrote it (don't delete a Terraform deployment's outputs)."""
    if not OUTPUTS_FILE.exists():
        return
    # An unreadable file is treated as ours (deployed_by None), so it is removed too.
    try:
        deployed_by = json.loads(OUTPUTS_FILE.read_text(encoding="utf-8")).get("deployed_by")
    except ValueError:
        deployed_by = None
    if deployed_by in ("boto3", None):
        OUTPUTS_FILE.unlink()
        info("removed .deploy/outputs.json")
    else:
        info(f".deploy/outputs.json belongs to a {deployed_by} deployment, left in place")
