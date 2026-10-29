"""scripts/demo_access.py: outcome evaluation with fake results (no network)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts import demo_access as demo


def call_result(text: str, is_error: bool = False, structured=None):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], is_error=is_error,
                           structured_content=structured)


def test_parse_only():
    assert demo.parse_only(None) == ["a", "b", "c", "d"]
    assert demo.parse_only("") == ["a", "b", "c", "d"]
    assert demo.parse_only("d, A") == ["a", "d"]
    with pytest.raises(ValueError, match="x"):
        demo.parse_only("a,x")


def test_run_order_puts_viewer_before_admin():
    assert set(demo.RUN_ORDER) == set(demo.SCENARIOS)
    assert demo.RUN_ORDER.index("b") < demo.RUN_ORDER.index("a")


# -- (a) alice --------------------------------------------------------------------------------

def test_admin_return_ok():
    o = demo.evaluate_admin_return({"response": "Done, RET-ABC123", "tools_used": [
        "orders___get_order", "orders___check_return_eligibility", "orders___create_return"]})
    assert o.ok and o.scenario == "a"


@pytest.mark.parametrize("result", [
    {"response": "It's already returned.", "tools_used": ["orders___get_order"]},
    {"response": "Error: boom", "tools_used": ["orders___create_return"], "error": "RuntimeError"},
    {"response": "x"},
])
def test_admin_return_unexpected(result):
    assert not demo.evaluate_admin_return(result).ok


# -- (b) bob ----------------------------------------------------------------------------------

def test_viewer_return_ok():
    o = demo.evaluate_viewer_return({
        "response": "Your headphones are eligible for a $199.99 refund, but your account doesn't have "
                    "permission to create returns.",
        "tools_used": ["orders___get_order", "orders___check_return_eligibility"],
    })
    assert o.ok and "AgentCore Policy" in o.why and "skill" in o.why


def test_viewer_return_fails_if_create_return_called():
    o = demo.evaluate_viewer_return({"response": "no permission", "tools_used": ["orders___create_return"]})
    assert not o.ok and "WAS called" in o.detail


def test_viewer_return_fails_without_permission_explanation():
    o = demo.evaluate_viewer_return({"response": "Sure, all done!", "tools_used": ["orders___get_order"]})
    assert not o.ok and "no permission explanation" in o.detail


# -- (c) carol --------------------------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 403])
def test_runtime_denied_ok(status):
    o = demo.evaluate_runtime_denied(None, demo.AccessDenied(status, "Forbidden"))
    assert o.ok and o.why.startswith("Runtime JWT authorizer") and str(status) in o.detail


def test_runtime_denied_unexpected():
    assert not demo.evaluate_runtime_denied({"response": "Hi carol!"}, None).ok
    assert not demo.evaluate_runtime_denied(None, RuntimeError("HTTP 500: oops")).ok


# -- (d) direct Gateway -----------------------------------------------------------------------

VIEWER = [t for t in demo.ALL_TOOLS if t not in demo.WRITE_TOOLS]


def test_tool_list_viewer_ok_and_leak():
    ok = demo.evaluate_tool_list("bob", VIEWER, VIEWER, demo.WRITE_TOOLS)
    assert ok.ok and "Policy" in ok.why and "hidden" in ok.detail
    leak = demo.evaluate_tool_list("bob", demo.ALL_TOOLS, VIEWER, demo.WRITE_TOOLS)
    assert not leak.ok and "orders___create_return" in leak.detail


def test_tool_list_admin_needs_all_seven():
    assert len(demo.ALL_TOOLS) == 7
    assert demo.evaluate_tool_list("alice", list(demo.ALL_TOOLS) + ["extra___x"], demo.ALL_TOOLS).ok
    missing = demo.evaluate_tool_list("alice", VIEWER, demo.ALL_TOOLS)
    assert not missing.ok and "support___create_ticket" in missing.detail


@pytest.mark.parametrize("result,error", [
    (None, RuntimeError("MCP error -32002: Tool call denied by policy")),
    (call_result("Unknown tool: orders___create_return", is_error=True), None),
    (call_result("Access denied: not authorized to call this tool"), None),
])
def test_direct_call_denied(result, error):
    o = demo.evaluate_direct_call("bob", result, error)
    assert o.ok and "Cedar" in o.why


@pytest.mark.parametrize("result", [
    call_result('{"return_id": "RET-1A2B3C", "status": "REQUESTED"}'),
    call_result("ok", structured={"return_id": "RET-1A2B3C"}),
    call_result('{"error": "NOT_ELIGIBLE", "reason": "already returned"}'),  # the tool ran
    call_result(""),
])
def test_direct_call_not_denied(result):
    assert not demo.evaluate_direct_call("bob", result, None).ok


def test_render_table_and_exit_code():
    outcomes = [
        demo.Outcome("d", "bob: Gateway tools/list", True, demo.WHY_POLICY_LIST, "5 tools"),
        demo.Outcome("a", "alice: agent creates the return", False, demo.WHY_ALLOWED, "tools: -"),
    ]
    table = demo.render_table(outcomes).splitlines()
    assert "enforced by" in table[0] and set(table[1]) == {"-"}
    assert table[2].startswith("❌") and " a " in table[2]  # sorted a..d
    assert table[3].startswith("✅") and demo.WHY_POLICY_LIST in table[3]
    assert demo.exit_code(outcomes) == 1
    assert demo.exit_code(outcomes[:1]) == 0
    assert demo.exit_code([]) == 1
