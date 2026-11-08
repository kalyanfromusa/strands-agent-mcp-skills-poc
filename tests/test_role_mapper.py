"""identity/role_mapper: Entra roles -> ShopAssist groups, allow-list only, highest wins, fail closed."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("role_mapper", ROOT / "identity" / "role_mapper" / "handler.py")
rm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rm)

MAP = {"AppRole-ShopAssist-Admin": "Group-ShopAssist-Admin", "AppRole-ShopAssist-Viewer": "Group-ShopAssist-Viewer"}
ENTRA = json.dumps([{"providerName": "EntraID", "providerType": "OIDC", "userId": "abc"}])


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENTRA_PROVIDER_NAME", "EntraID")
    monkeypatch.setenv("ENTRA_ROLE_SOURCE", "roles")
    monkeypatch.setenv("ENTRA_ROLE_MAP", json.dumps(MAP))


def event(roles: str | None, identities: str | None = ENTRA, version: str = "1") -> dict:
    attrs = {"sub": "u-1"}
    if identities is not None:
        attrs["identities"] = identities
    if roles is not None:
        attrs["custom:entra_roles"] = roles
    return {"version": version, "triggerSource": "TokenGeneration_HostedAuth", "request": {"userAttributes": attrs}, "response": {}}


def groups_of(out: dict) -> list[str]:
    r = out["response"]
    details = r.get("claimsOverrideDetails") or r.get("claimsAndScopeOverrideDetails")
    return details["groupOverrideDetails"]["groupsToOverride"]


@pytest.mark.parametrize("raw,expected", [
    ('["AppRole-ShopAssist-Admin"]', ["Group-ShopAssist-Admin"]),
    ("[AppRole-ShopAssist-Viewer]", ["Group-ShopAssist-Viewer"]),
    ("AppRole-ShopAssist-Viewer, AppRole-ShopAssist-Admin", ["Group-ShopAssist-Admin"]),  # highest wins
    ('["Something-Else"]', []),  # not on the allow-list
    ("", []),
    (None, []),
])
def test_mapping(raw, expected):
    assert groups_of(rm.lambda_handler(event(raw), None)) == expected


def test_cognito_users_untouched():
    out = rm.lambda_handler(event('["AppRole-ShopAssist-Admin"]', identities=None), None)
    assert out["response"] == {}


def test_other_provider_untouched():
    other = json.dumps([{"providerName": "SomeOtherIdP"}])
    assert rm.lambda_handler(event('["AppRole-ShopAssist-Admin"]', identities=other), None)["response"] == {}


def test_trigger_v2_shape():
    out = rm.lambda_handler(event('["AppRole-ShopAssist-Admin"]', version="2"), None)
    assert "claimsAndScopeOverrideDetails" in out["response"]
    assert groups_of(out) == ["Group-ShopAssist-Admin"]


def test_groups_mode_uses_object_ids(monkeypatch):
    monkeypatch.setenv("ENTRA_ROLE_SOURCE", "groups")
    monkeypatch.setenv("ENTRA_ROLE_MAP", json.dumps({"11111111-aaaa": "Group-ShopAssist-Viewer"}))
    e = event(None)
    e["request"]["userAttributes"]["custom:entra_groups"] = '["11111111-aaaa","22222222-bbbb"]'
    assert groups_of(rm.lambda_handler(e, None)) == ["Group-ShopAssist-Viewer"]


def test_garbled_identities_fail_safe():
    assert rm.lambda_handler(event('["AppRole-ShopAssist-Admin"]', identities="not json"), None)["response"] == {}
