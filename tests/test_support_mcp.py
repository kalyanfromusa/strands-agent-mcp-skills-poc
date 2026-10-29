"""Support MCP tools, the support server's tool listing, and the local Gateway emulator."""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from mcp.client import Client

from common.store import get_store
from support_mcp import server as support

ALL_GATEWAY_TOOLS = {
    "orders___get_order", "orders___list_customer_orders", "orders___check_return_eligibility",
    "orders___create_return", "support___create_ticket", "support___get_ticket", "support___search_faq",
}
VIEWER_TOOLS = {"orders___get_order", "orders___list_customer_orders", "orders___check_return_eligibility",
                "support___get_ticket", "support___search_faq"}


@pytest.fixture(autouse=True)
def memory_store(monkeypatch):
    monkeypatch.setenv("STORE", "memory")
    get_store.cache_clear()
    yield
    get_store.cache_clear()


def run(coro):
    return asyncio.run(coro)


async def list_tools(server) -> dict:
    async with Client(server) as client:
        return {t.name: t for t in (await client.list_tools()).tools}


async def call_tool(server, name: str, args: dict):
    async with Client(server) as client:
        return await client.call_tool(name, args)


# --- plain tool functions -------------------------------------------------------------------

def test_create_and_get_ticket():
    created = support.create_ticket("c002", "Speaker crackles", "Crackling at high volume.", order_id="1003",
                                    priority="high")
    assert re.fullmatch(r"TCK-[0-9A-F]{6}", created["ticket_id"])
    assert created["status"] == "OPEN" and created["created_at"].endswith("+00:00")
    ticket = support.get_ticket(created["ticket_id"].lower())
    assert ticket["customer_id"] == "C002" and ticket["order_id"] == "1003" and ticket["priority"] == "high"
    assert support.get_ticket("TCK-NOPE00") == {"error": "TICKET_NOT_FOUND", "ticket_id": "TCK-NOPE00"}


def test_create_ticket_defaults_and_validation():
    ticket = support.get_ticket(support.create_ticket("C001", "Question", "Hello")["ticket_id"])
    assert ticket["priority"] == "medium" and ticket["order_id"] == ""
    assert support.create_ticket("C001", "x", "y", priority="urgent")["error"] == "INVALID_PRIORITY"


@pytest.mark.parametrize(("query", "expected"), [
    ("how long does shipping take", "How long does shipping take?"),
    ("when will I get my refund", "When will I get my refund?"),
    ("my item arrived broken", "My item arrived damaged or defective. What should I do?"),
    ("can I pay with PayPal?", "Which payment methods do you accept?"),
    ("change the address on my order", "Can I change or cancel my order?"),
    ("phone number to call support", "How can I contact customer support?"),
    ("return policy", "What is your return policy?"),
])
def test_search_faq_top_hit(query, expected):
    results = support.search_faq(query)["results"]
    assert 1 <= len(results) <= 3
    assert results[0]["question"] == expected
    assert set(results[0]) == {"question", "answer"}


def test_search_faq_no_match_and_size():
    assert support.search_faq("quantum chromodynamics")["results"] == []
    assert len(support.load_faq()) >= 8


# --- MCP surface ----------------------------------------------------------------------------

def test_support_server_lists_tools_with_text_output():
    tools = run(list_tools(support.mcp))
    assert set(tools) == {"create_ticket", "get_ticket", "search_faq"}
    schema = tools["create_ticket"].input_schema
    assert set(schema["required"]) == {"customer_id", "subject", "description"}
    assert schema["properties"]["priority"]["enum"] == ["low", "medium", "high"]
    result = run(call_tool(support.mcp, "search_faq", {"query": "shipping"}))
    assert not result.is_error
    assert json.loads(result.content[0].text)["results"]


def test_local_gateway_lists_all_prefixed_tools():
    from scripts.local_gateway import build_server

    tools = run(list_tools(build_server()))
    assert set(tools) == ALL_GATEWAY_TOOLS
    # orders schemas come from tools_schema.json (as the real Gateway's inline schema)
    assert tools["orders___create_return"].input_schema["required"] == ["order_id", "sku", "reason"]
    assert "re-checked" in tools["orders___create_return"].description


def test_local_gateway_calls_return_json_text():
    from scripts.local_gateway import build_server

    server = build_server("admin")
    result = run(call_tool(server, "orders___check_return_eligibility", {"order_id": "1042", "sku": "EL-HP-100"}))
    assert not result.is_error
    assert json.loads(result.content[0].text)["eligible"] is True
    created = json.loads(run(call_tool(server, "orders___create_return",
                                       {"order_id": "1042", "sku": "EL-HP-100", "reason": "too big"})).content[0].text)
    assert created["status"] == "REQUESTED"
    ticket = json.loads(run(call_tool(server, "support___create_ticket",
                                      {"customer_id": "C001", "subject": "s", "description": "d"})).content[0].text)
    fetched = json.loads(run(call_tool(server, "support___get_ticket", {"ticket_id": ticket["ticket_id"]})).content[0].text)
    assert fetched["ticket_id"] == ticket["ticket_id"]


def test_local_gateway_viewer_role_filters_and_rejects():
    from scripts.local_gateway import build_server

    server = build_server("viewer")
    assert set(run(list_tools(server))) == VIEWER_TOOLS
    for name, args in (("orders___create_return", {"order_id": "1042", "sku": "EL-HP-100", "reason": "x"}),
                       ("support___create_ticket", {"customer_id": "C001", "subject": "s", "description": "d"})):
        result = run(call_tool(server, name, args))
        assert result.is_error
    assert get_store().returns == {} and get_store().tickets == {}
    ok = run(call_tool(server, "orders___get_order", {"order_id": "1042"}))
    assert json.loads(ok.content[0].text)["order_id"] == "1042"
    with pytest.raises(ValueError):
        build_server("root")
