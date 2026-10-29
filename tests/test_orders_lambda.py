"""Orders Lambda: Gateway tool routing, tool outputs, create_return re-checking eligibility."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.store import get_store
from orders_lambda import handler

SCHEMA = Path(__file__).resolve().parents[1] / "mcp_servers" / "orders_lambda" / "tools_schema.json"


def ctx(tool: str, target: str = "orders"):
    """Fake Lambda context as sent by AgentCore Gateway."""
    return SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": f"{target}___{tool}"}))


def call(tool: str, **args):
    result = handler.lambda_handler(args, ctx(tool))
    json.dumps(result)  # must be JSON-serialisable
    return result


@pytest.fixture(autouse=True)
def memory_store(monkeypatch):
    monkeypatch.setenv("STORE", "memory")
    get_store.cache_clear()
    yield
    get_store.cache_clear()


def test_schema_matches_tools():
    specs = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert [s["name"] for s in specs] == list(handler.TOOLS)
    for spec in specs:
        schema = spec["inputSchema"]
        assert schema["type"] == "object" and len(spec["description"]) > 40
        assert set(schema["required"]) <= set(schema["properties"])


@pytest.mark.parametrize(("raw", "expected"), [
    ("orders___get_order", "get_order"), ("my-target___create_return", "create_return"), ("get_order", "get_order"),
])
def test_tool_name_prefix_is_stripped(raw, expected):
    context = SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": raw}))
    assert handler.tool_name_from_context(context) == expected


def test_get_order():
    order = call("get_order", order_id="1042")
    assert order["order_id"] == "1042" and order["status"] == "DELIVERED"
    assert order["items"][0]["sku"] == "EL-HP-100"
    assert call("get_order", order_id="9999")["error"] == "ORDER_NOT_FOUND"


def test_list_customer_orders_newest_first():
    result = call("list_customer_orders", customer_id="c001")
    assert result["customer_id"] == "C001"
    assert {o["order_id"] for o in result["orders"]} == {"1001", "1002", "1007", "1042"}
    dates = [o["order_date"] for o in result["orders"]]
    assert dates == sorted(dates, reverse=True)
    assert set(result["orders"][0]) == {"order_id", "status", "order_date", "total"}
    assert call("list_customer_orders", customer_id="C999")["orders"] == []


def test_check_return_eligibility_routes_to_rules():
    assert call("check_return_eligibility", order_id="1042", sku="EL-HP-100")["eligible"] is True
    assert call("check_return_eligibility", order_id="1004", sku="EL-WCH-030")["eligible"] is False
    assert call("check_return_eligibility", order_id="9999", sku="X")["eligible"] is False


def test_create_return_then_item_is_no_longer_eligible():
    created = call("create_return", order_id="1042", sku="EL-HP-100", reason="too tight")
    assert re.fullmatch(r"RET-[0-9A-F]{6}", created["return_id"])
    assert created["status"] == "REQUESTED"
    assert created["refund_amount"] == pytest.approx(199.99)
    assert created["label_url"].endswith(f"{created['return_id']}.pdf")
    stored = get_store().returns[created["return_id"]]
    assert stored["order_id"] == "1042" and stored["reason"] == "too tight"
    # Server-side re-check: a second return of the same item is refused.
    again = call("create_return", order_id="1042", sku="EL-HP-100", reason="again")
    assert again["error"] == "NOT_ELIGIBLE" and "already been returned" in again["reason"]


@pytest.mark.parametrize(("order_id", "sku"), [
    ("1002", "HM-LMP-010"), ("1004", "EL-WCH-030"), ("1005", "FS-SWM-040"), ("1006", "HM-MUG-050"),
    ("1007", "CL-HAT-060"), ("1042", "NOPE"), ("9999", "EL-HP-100"),
])
def test_create_return_rechecks_eligibility(order_id, sku):
    result = call("create_return", order_id=order_id, sku=sku, reason="changed my mind")
    assert result["error"] == "NOT_ELIGIBLE" and result["reason"]
    assert get_store().returns == {}


def test_unknown_tool_and_bad_input():
    assert handler.lambda_handler({}, ctx("delete_everything")) == {"error": "UNKNOWN_TOOL", "tool": "delete_everything"}
    assert handler.lambda_handler({}, SimpleNamespace(client_context=None))["error"] == "UNKNOWN_TOOL"
    bad = handler.lambda_handler({"order_id": "1042"}, ctx("create_return"))
    assert bad["error"] == "INVALID_INPUT"
    assert handler.lambda_handler({"order_id": "1042", "x": 1}, ctx("get_order"))["error"] == "INVALID_INPUT"
