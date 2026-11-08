"""Cognito pre-token-generation trigger: turn a federated user's Entra roles into ShopAssist groups.

What this file does
    Cognito calls this Lambda every time it issues tokens (sign-in and refresh). For users who signed in
    through the Entra ID identity provider, it reads the roles Entra sent (stored by Cognito in the
    custom attribute `custom:entra_roles`) and writes the matching ShopAssist group into the token's
    `cognito:groups` claim:
        AppRole-ShopAssist-Admin  -> Group-ShopAssist-Admin   (all tools)
        AppRole-ShopAssist-Viewer -> Group-ShopAssist-Viewer  (read-only tools)
    Cognito's own users (alice, bob, carol) pass through untouched: their groups are real Cognito groups.

Where it fits
    The AgentCore Runtime (who may use the agent) and the Cedar policies at the Gateway (which tools)
    only look at `cognito:groups`. This trigger is the one place where Entra's vocabulary is translated
    into ours, at the trust boundary, so nothing downstream knows or cares where a user came from.
    Created by infra/terraform/modules/role_mapper; the mapping comes from Terraform (ENTRA_ROLE_MAP).

Step by step
    1. Is this a federated Entra user? (the `identities` attribute names provider ENTRA_PROVIDER_NAME)
       If not, return the event unchanged.
    2. Read the values: `custom:entra_roles` (ENTRA_ROLE_SOURCE=roles) or `custom:entra_groups`
       (=groups, the Entra-Free fallback: group object ids). They can arrive as a JSON array string,
       "[a, b]", or comma/space separated text.
    3. Keep only values listed in ENTRA_ROLE_MAP (an allow-list); unknown values are ignored.
    4. The highest mapped group wins (Admin over Viewer), so a token carries exactly one ShopAssist group.
    5. Override `cognito:groups` with that group, or with no group at all when nothing matched
       (fail closed: the Runtime then refuses the user, like carol). Works for trigger versions 1 and 2.
    6. Log the decision (user sub, values seen, group chosen). Tokens are never logged.

Key terms
    Pre-token-generation trigger: a Lambda Cognito runs just before it creates tokens, able to change claims.
    Federated user: someone who signs in with another identity provider (here Entra ID) through Cognito.
    Fail closed: when in doubt, grant nothing.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Highest first: if a user maps to several groups, the first one in this list wins.
GROUP_RANK = ["Group-ShopAssist-Admin", "Group-ShopAssist-Viewer"]


def settings() -> tuple[str, str, dict[str, str]]:
    """Read the configuration (environment variables set by Terraform)."""
    provider = os.environ.get("ENTRA_PROVIDER_NAME", "EntraID")
    source = os.environ.get("ENTRA_ROLE_SOURCE", "roles")
    mapping = json.loads(os.environ.get("ENTRA_ROLE_MAP", "{}"))
    return provider, source, mapping


def is_federated(attrs: dict[str, str], provider: str) -> bool:
    """Step 1: the `identities` attribute is a JSON list like [{"providerName": "EntraID", ...}]."""
    try:
        identities = json.loads(attrs.get("identities") or "[]")
    except json.JSONDecodeError:
        return False
    return any(isinstance(i, dict) and i.get("providerName") == provider for i in identities)


def parse_values(raw: str | None) -> list[str]:
    """Step 2: '["a","b"]' or '[a, b]' or 'a,b' or 'a b' -> ['a', 'b']."""
    if not raw:
        return []
    raw = raw.strip()
    try:
        value = json.loads(raw)
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str):
            raw = value
    except json.JSONDecodeError:
        pass
    return [v for v in re.split(r"[\s,\[\]\"']+", raw) if v]


def choose_group(values: list[str], mapping: dict[str, str]) -> str | None:
    """Steps 3-4: allow-listed values only; the highest-ranked group wins; None = no access."""
    groups = {mapping[v] for v in values if v in mapping}
    for g in GROUP_RANK:
        if g in groups:
            return g
    return None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Entry point Cognito calls. Returns the event with the token-claim overrides filled in."""
    provider, source, mapping = settings()
    attrs = event.get("request", {}).get("userAttributes", {}) or {}

    # Step 1: Cognito users keep their real groups.
    if not is_federated(attrs, provider):
        return event

    # Steps 2-4.
    attribute = "custom:entra_groups" if source == "groups" else "custom:entra_roles"
    values = parse_values(attrs.get(attribute))
    group = choose_group(values, mapping)
    groups = [group] if group else []

    # Step 5: write the override in the shape of the trigger version Cognito called us with.
    override = {"groupsToOverride": groups, "iamRolesToOverride": [], "preferredRole": None}
    response = event.setdefault("response", {})
    if str(event.get("version", "1")) == "1":
        response["claimsOverrideDetails"] = {"groupOverrideDetails": override}
    else:
        response["claimsAndScopeOverrideDetails"] = {"groupOverrideDetails": override}

    # Step 6.
    log.info(json.dumps({
        "event": "role_mapping",
        "sub": attrs.get("sub"),
        "trigger": event.get("triggerSource"),
        "source": attribute,
        "values": values[:10],
        "group": group,
    }))
    return event
