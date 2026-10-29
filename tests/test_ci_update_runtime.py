"""scripts/ci/update_runtime_image.py: request building checked offline against the installed botocore model.

No AWS calls: clients are created with a region and dummy keys only, and the update/sync flows run
against a small fake client.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import boto3
import pytest
from botocore.validate import ParamValidator

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("update_runtime_image", ROOT / "scripts" / "ci" / "update_runtime_image.py")
uri = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(uri)

REGION, ACCOUNT = "us-east-1", "123456789012"
IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/shopassist-agent:0123abc"
RUNTIME_ID = "shopassist_agent-AbC123xyz9"


def _model():
    return boto3.client("bedrock-agentcore-control", region_name=REGION, aws_access_key_id="x",
                        aws_secret_access_key="x").meta.service_model


def _validate(op: str, params: dict) -> None:
    shape = _model().operation_model(op).input_shape
    report = ParamValidator().validate(params, shape)
    assert not report.has_errors(), report.generate_report()


def _get_response(artifact: dict | None = None) -> dict:
    """A realistic GetAgentRuntime response for the agent runtime (as deployed by Terraform/boto3)."""
    return {
        "ResponseMetadata": {"HTTPStatusCode": 200},
        "agentRuntimeArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME_ID}",
        "agentRuntimeName": "shopassist_agent",
        "agentRuntimeId": RUNTIME_ID,
        "agentRuntimeVersion": "3",
        "status": "READY",
        "createdAt": "2026-10-29T10:00:00Z",
        "lastUpdatedAt": "2026-10-29T11:00:00Z",
        "workloadIdentityDetails": {"workloadIdentityArn": "arn:aws:bedrock-agentcore:...:workload-identity/x"},
        "description": "Generic skills-driven Strands agent",
        "roleArn": f"arn:aws:iam::{ACCOUNT}:role/shopassist-agent-runtime-role",
        "agentRuntimeArtifact": artifact or {"containerConfiguration": {"containerUri": IMAGE.replace("0123abc", "old")}},
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "authorizerConfiguration": {"customJWTAuthorizer": {
            "discoveryUrl": f"https://cognito-idp.{REGION}.amazonaws.com/{REGION}_Pool/.well-known/openid-configuration",
            "allowedClients": ["usersclient"],
            "customClaims": [{
                "inboundTokenClaimName": "cognito:groups",
                "inboundTokenClaimValueType": "STRING_ARRAY",
                "authorizingClaimMatchValue": {"claimMatchValue": {"matchValueStringList": ["shop-admin", "shop-viewer"]},
                                               "claimMatchOperator": "CONTAINS_ANY"},
            }],
        }},
        "requestHeaderConfiguration": {"requestHeaderAllowlist": ["Authorization"]},
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
        "environmentVariables": {"MODEL_ID": "us.anthropic.claude-haiku-5-5", "GATEWAY_AUTH_MODE": "passthrough",
                                 "SKILLS_S3_URI": "s3://b/skills/"},
    }


# ------------------------------------------------------------------ model contract


def test_preserved_fields_cover_every_updatable_field_get_returns():
    """Every UpdateAgentRuntime input also returned by GetAgentRuntime is copied (or is id/artifact)."""
    m = _model()
    update_in = set(m.operation_model("UpdateAgentRuntime").input_shape.members)
    get_out = set(m.operation_model("GetAgentRuntime").output_shape.members)
    expected = (update_in & get_out) - {"agentRuntimeId", "agentRuntimeArtifact"}
    assert set(uri.PRESERVED_FIELDS) == expected


def test_preserved_fields_have_identical_shapes_in_get_and_update():
    """Copying Get output into Update input is only safe if both sides use the same shape."""
    m = _model()
    update_in = m.operation_model("UpdateAgentRuntime").input_shape.members
    get_out = m.operation_model("GetAgentRuntime").output_shape.members
    for key in uri.PRESERVED_FIELDS:
        assert update_in[key].name == get_out[key].name, key


def test_container_configuration_shape():
    art = _model().shape_for("AgentRuntimeArtifact")
    assert "containerConfiguration" in art.members
    assert art.members["containerConfiguration"].required_members == ["containerUri"]


def test_status_names_exist_in_model():
    m = _model()
    assert uri.RUNTIME_FAILED <= set(m.shape_for("AgentRuntimeStatus").enum)
    assert {"READY"} | uri.TARGET_FAILED | uri.TARGET_PENDING_AUTH <= set(m.shape_for("TargetStatus").enum)
    assert {"READY", "UPDATE_FAILED"} <= set(m.shape_for("AgentRuntimeEndpointStatus").enum)


# ------------------------------------------------------------------ request building


def test_update_request_swaps_only_the_artifact_and_validates():
    current = _get_response()
    req = uri.build_update_request(current, IMAGE)
    _validate("UpdateAgentRuntime", req)
    assert req["agentRuntimeArtifact"] == {"containerConfiguration": {"containerUri": IMAGE}}
    assert req["agentRuntimeId"] == RUNTIME_ID
    for key in ("roleArn", "networkConfiguration", "protocolConfiguration", "authorizerConfiguration",
                "requestHeaderConfiguration", "environmentVariables", "lifecycleConfiguration", "description"):
        assert req[key] == current[key], key
    # Read-only fields never leak into the request.
    for key in ("status", "agentRuntimeArn", "agentRuntimeName", "agentRuntimeVersion", "createdAt",
                "workloadIdentityDetails", "ResponseMetadata"):
        assert key not in req


def test_update_request_from_code_mode_replaces_code_configuration():
    code = {"codeConfiguration": {"code": {"s3": {"bucket": "b", "prefix": "artifacts/agent.zip"}},
                                  "runtime": "PYTHON_3_12", "entryPoint": ["app.py"]}}
    req = uri.build_update_request(_get_response(code), IMAGE)
    _validate("UpdateAgentRuntime", req)
    assert "codeConfiguration" not in req["agentRuntimeArtifact"]


def test_update_request_mcp_runtime_without_optional_fields():
    current = _get_response()
    for key in ("requestHeaderConfiguration", "lifecycleConfiguration"):
        del current[key]
    current["protocolConfiguration"] = {"serverProtocol": "MCP"}
    current["authorizerConfiguration"] = {"customJWTAuthorizer": {"discoveryUrl": "https://x/.well-known/openid-configuration",
                                                                  "allowedClients": ["m2m"]}}
    req = uri.build_update_request(current, IMAGE.replace("shopassist-agent", "shopassist-support-mcp"))
    _validate("UpdateAgentRuntime", req)
    assert "requestHeaderConfiguration" not in req
    assert req["protocolConfiguration"] == {"serverProtocol": "MCP"}


@pytest.mark.parametrize("bad", ["", "shopassist-agent", f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/shopassist-agent"])
def test_update_request_rejects_untagged_images(bad):
    with pytest.raises(ValueError):
        uri.build_update_request(_get_response(), bad)


def test_update_request_accepts_digest():
    digest = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/shopassist-agent@sha256:" + "a" * 64
    _validate("UpdateAgentRuntime", uri.build_update_request(_get_response(), digest))


def test_sync_request_validates():
    req = uri.build_sync_request("shopassist-gateway-abc123", "TGT123")
    _validate("SynchronizeGatewayTargets", req)
    assert uri.find_target_id([{"name": "orders", "targetId": "A"}, {"name": "support", "targetId": "B"}], "support") == "B"
    with pytest.raises(SystemExit):
        uri.find_target_id([{"name": "orders", "targetId": "A"}], "support")


# ------------------------------------------------------------------ flows against a fake client


class FakeAgentCore:
    """Just enough of bedrock-agentcore-control: one runtime that becomes READY at the new version."""

    def __init__(self, current: dict):
        self.current = current
        self.calls: list[tuple[str, dict]] = []

    def get_agent_runtime(self, **kw):
        _validate("GetAgentRuntime", kw)
        return self.current

    def update_agent_runtime(self, **kw):
        _validate("UpdateAgentRuntime", kw)
        self.calls.append(("update", kw))
        self.current = {**self.current, "agentRuntimeArtifact": kw["agentRuntimeArtifact"],
                        "agentRuntimeVersion": "4", "status": "READY"}
        return {"agentRuntimeVersion": "4", "status": "UPDATING"}

    def get_agent_runtime_endpoint(self, **kw):
        _validate("GetAgentRuntimeEndpoint", kw)
        return {"status": "READY", "liveVersion": self.current["agentRuntimeVersion"]}

    def list_gateway_targets(self, **kw):
        _validate("ListGatewayTargets", kw)
        return {"items": [{"name": "orders", "targetId": "T1"}, {"name": "support", "targetId": "T2"}]}

    def synchronize_gateway_targets(self, **kw):
        _validate("SynchronizeGatewayTargets", kw)
        self.calls.append(("sync", kw))
        return {"targets": []}

    def get_gateway_target(self, **kw):
        _validate("GetGatewayTarget", kw)
        return {"status": "READY"}


def test_update_flow_updates_and_waits():
    ac = FakeAgentCore(_get_response())
    args = uri.parse_args(["update-runtime", "--runtime-id", RUNTIME_ID, "--image", IMAGE])
    assert uri.cmd_update_runtime(args, ac) == 0
    assert [c[0] for c in ac.calls] == ["update"]


def test_update_flow_is_a_noop_for_the_same_image():
    ac = FakeAgentCore(_get_response({"containerConfiguration": {"containerUri": IMAGE}}))
    args = uri.parse_args(["update-runtime", "--runtime-id", RUNTIME_ID, "--image", IMAGE])
    assert uri.cmd_update_runtime(args, ac) == 0
    assert ac.calls == []


def test_update_flow_refuses_code_mode_without_flag():
    code = {"codeConfiguration": {"code": {"s3": {"bucket": "b", "prefix": "k"}}, "runtime": "PYTHON_3_12",
                                  "entryPoint": ["app.py"]}}
    ac = FakeAgentCore(_get_response(code))
    with pytest.raises(SystemExit):
        uri.cmd_update_runtime(uri.parse_args(["update-runtime", "--runtime-id", RUNTIME_ID, "--image", IMAGE]), ac)
    args = uri.parse_args(["update-runtime", "--runtime-id", RUNTIME_ID, "--image", IMAGE, "--allow-mode-switch"])
    assert uri.cmd_update_runtime(args, ac) == 0


def test_sync_flow_looks_up_target_by_name():
    ac = FakeAgentCore(_get_response())
    args = uri.parse_args(["sync-target", "--gateway-id", "shopassist-gateway-abc", "--target-name", "support"])
    assert uri.cmd_sync_target(args, ac) == 0
    assert ac.calls == [("sync", {"gatewayIdentifier": "shopassist-gateway-abc", "targetIdList": ["T2"]})]
