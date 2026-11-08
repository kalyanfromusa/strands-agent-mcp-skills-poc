"""scripts/tf_plan_summary.py: actions are classified right, values never leak, the hash is stable."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("tf_plan_summary", ROOT / "scripts" / "tf_plan_summary.py")
tps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tps)

PLAN = {
    "resource_changes": [
        {"address": "module.data.aws_dynamodb_table.orders", "change": {"actions": ["create"], "after": {"name": "SECRET-NAME"}}},
        {"address": "module.agent_runtime.aws_bedrockagentcore_agent_runtime.this", "change": {"actions": ["update"],
         "after": {"environment_variables": {"TOKEN": "super-secret-value"}}}},
        {"address": "module.identity.random_password.user[\"alice\"]", "change": {"actions": ["delete", "create"], "after": {"result": "p@ss"}}},
        {"address": "aws_budgets_budget.monthly[0]", "change": {"actions": ["delete"]}},
        {"address": "data.aws_caller_identity.current", "change": {"actions": ["read"]}},
        {"address": "module.gateway.aws_iam_role.this", "change": {"actions": ["no-op"]}},
    ]
}


def test_actions_and_counts():
    items = tps.changes(PLAN)
    assert dict(items) == {
        "module.data.aws_dynamodb_table.orders": "create",
        "module.agent_runtime.aws_bedrockagentcore_agent_runtime.this": "update",
        'module.identity.random_password.user["alice"]': "replace",
        "aws_budgets_budget.monthly[0]": "delete",
    }
    md = tps.markdown(items, "t")
    assert "1 to add, 1 to change, 1 to replace, 1 to destroy" in md
    assert "replaces or destroys" in md


def test_no_values_leak():
    md = tps.markdown(tps.changes(PLAN), "t")
    for secret in ("SECRET-NAME", "super-secret-value", "p@ss", "TOKEN"):
        assert secret not in md


def test_hash_is_order_independent_and_change_sensitive():
    items = tps.changes(PLAN)
    reordered = {"resource_changes": list(reversed(PLAN["resource_changes"]))}
    assert tps.summary_hash(items) == tps.summary_hash(tps.changes(reordered))
    fewer = {"resource_changes": PLAN["resource_changes"][1:]}
    assert tps.summary_hash(items) != tps.summary_hash(tps.changes(fewer))


def test_helper_replace_is_a_rerun_not_a_warning():
    plan = {"resource_changes": [
        {"address": "terraform_data.sync_gateway_targets", "type": "terraform_data", "change": {"actions": ["delete", "create"]}},
        {"address": "module.agent_runtime.aws_bedrockagentcore_agent_runtime.this", "type": "aws_bedrockagentcore_agent_runtime", "change": {"actions": ["update"]}},
    ]}
    items = tps.changes(plan)
    assert dict(items)["terraform_data.sync_gateway_targets"] == "rerun"
    md = tps.markdown(items, "t")
    assert "replaces or destroys" not in md
    assert "1 helper step(s) re-run" in md


def test_empty_plan():
    assert "No changes." in tps.markdown(tps.changes({}), "t")
