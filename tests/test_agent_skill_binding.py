"""Generic agent: skills loading and the hybrid skill-to-tool binding (no network, no LLM)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from strands.vended_plugins.skills import Skill  # noqa: E402

import skill_binding as sb  # noqa: E402

ALL_TOOLS = {
    "orders___get_order": "Get one order by id.",
    "orders___list_customer_orders": "List a customer's orders.",
    "orders___check_return_eligibility": "Check whether an item can be returned.",
    "orders___create_return": "Create a return.",
    "support___create_ticket": "Open a ticket.",
    "support___get_ticket": "Get a ticket.",
    "support___search_faq": "Search the FAQ.",
}
VIEWER_TOOLS = {k: v for k, v in ALL_TOOLS.items() if k not in ("orders___create_return", "support___create_ticket")}
LOCAL_TOOLS = ["skills", "read_skill_resource"]


@pytest.fixture(scope="module")
def skills() -> dict[str, Skill]:
    return {s.name: s for s in Skill.from_directory(ROOT / "skills")}


def test_skills_load_from_directory(skills):
    folders = sorted(p.name for p in (ROOT / "skills").iterdir() if (p / "SKILL.md").is_file())
    assert len(skills) == 3
    assert sorted(skills) == folders == ["order-tracking", "returns-and-refunds", "support-ticketing"]
    for name, skill in skills.items():
        assert skill.description and skill.instructions
        assert skill.path is not None and Path(skill.path).name == name


def test_declared_servers(skills):
    assert sb.declared_servers(skills["order-tracking"]) == ["orders"]
    assert sb.declared_servers(skills["returns-and-refunds"]) == ["orders", "support"]
    assert sb.declared_servers(skills["support-ticketing"]) == ["support"]


def test_declared_servers_variants():
    assert sb.declared_servers(Skill(name="x", description="d")) == []
    assert sb.declared_servers(Skill(name="x", description="d", metadata={"mcp-servers": " a  b a "})) == ["a", "b"]
    assert sb.declared_servers(Skill(name="x", description="d", metadata={"mcp-servers": ["a", "c"]})) == ["a", "c"]


def test_split_gateway_name():
    assert sb.split_gateway_name("orders___get_order") == ("orders", "get_order")
    assert sb.split_gateway_name("read_skill_resource") is None
    assert sb.split_gateway_name("___x") is None
    assert sb.split_gateway_name("x___") is None


def test_plain_tool_refs():
    body = "Call `get_thing`, then `get_thing` again, `read_skill_resource`, but not `PROCESSING` or `high`."
    assert sb.plain_tool_refs(body) == ["get_thing", "read_skill_resource"]
    assert sb.plain_tool_refs(body, ignore=["read_skill_resource"]) == ["get_thing"]


# -- decide_tool_call --------------------------------------------------------------------------

SERVER_SKILLS = {"orders": ["order-tracking", "returns-and-refunds"], "support": ["returns-and-refunds", "support-ticketing"]}
KNOWN = list(ALL_TOOLS) + LOCAL_TOOLS


def test_gateway_tool_blocked_without_activated_skill():
    msg = sb.decide_tool_call("orders___get_order", set(), SERVER_SKILLS, KNOWN)
    assert msg is not None and "blocked" in msg and "'orders'" in msg
    assert "candidates: order-tracking, returns-and-refunds" in msg


def test_gateway_tool_blocked_when_only_other_server_active():
    msg = sb.decide_tool_call("orders___create_return", {"support"}, SERVER_SKILLS, KNOWN)
    assert msg is not None and "candidates" in msg


def test_gateway_tool_on_undeclared_server_has_no_candidates():
    msg = sb.decide_tool_call("billing___charge", {"orders"}, SERVER_SKILLS, KNOWN)
    assert msg is not None and "No available skill declares this server" in msg


def test_gateway_tool_allowed_with_activated_skill():
    assert sb.decide_tool_call("orders___get_order", {"orders"}, SERVER_SKILLS, KNOWN) is None
    assert sb.decide_tool_call("support___search_faq", {"orders", "support"}, SERVER_SKILLS, KNOWN) is None


def test_plain_name_hint():
    usable = sb.decide_tool_call("create_return", {"orders"}, SERVER_SKILLS, KNOWN)
    assert usable == "Unknown tool 'create_return'. Use the exact name: orders___create_return."
    inactive = sb.decide_tool_call("create_return", set(), SERVER_SKILLS, KNOWN)
    assert inactive is not None and "orders___create_return" in inactive and "Activate the right skill" in inactive


def test_non_gateway_tools_allowed():
    for name in LOCAL_TOOLS:
        assert sb.decide_tool_call(name, set(), SERVER_SKILLS, KNOWN) is None
    # Unknown plain names that match nothing are left to the framework.
    assert sb.decide_tool_call("something_else", set(), SERVER_SKILLS, KNOWN) is None


# -- resolution_text ---------------------------------------------------------------------------


def test_resolution_text_all_tools(skills):
    text = sb.resolution_text(skills["returns-and-refunds"], ALL_TOOLS, LOCAL_TOOLS)
    lines = text.splitlines()
    assert lines[0].startswith("[Tool binding]")
    listed = [line[2:].split(" — ")[0] for line in lines if line.startswith("- ")]
    assert listed == sorted(ALL_TOOLS)  # both declared servers, exact prefixed names
    assert "Not available" not in text and "permission" not in text


def test_resolution_text_only_declared_server(skills):
    text = sb.resolution_text(skills["order-tracking"], ALL_TOOLS, LOCAL_TOOLS)
    assert "- orders___get_order — Get one order by id." in text
    assert "support___" not in text


def test_resolution_text_marks_missing_tools_for_viewer(skills):
    text = sb.resolution_text(skills["returns-and-refunds"], VIEWER_TOOLS, LOCAL_TOOLS)
    assert "orders___create_return" not in text and "support___create_ticket" not in text
    assert "Not available to the current user: create_ticket, create_return" in text
    assert "doesn't have permission" in text
    support = sb.resolution_text(skills["support-ticketing"], VIEWER_TOOLS, LOCAL_TOOLS)
    assert "Not available to the current user: create_ticket" in support


def test_resolution_text_no_tools_on_server(skills):
    no_support = {k: v for k, v in ALL_TOOLS.items() if k.startswith("orders___")}
    text = sb.resolution_text(skills["support-ticketing"], no_support, LOCAL_TOOLS)
    assert "- (none)" in text
    assert "No tools available to the current user on server(s): support" in text


def test_resolution_text_without_servers():
    text = sb.resolution_text(Skill(name="plain", description="d"), ALL_TOOLS)
    assert "declares no MCP servers" in text


def test_long_descriptions_are_shortened(skills):
    tools = {**ALL_TOOLS, "orders___get_order": "word " * 100}
    line = next(l for l in sb.resolution_text(skills["order-tracking"], tools).splitlines() if "get_order" in l)
    assert line.endswith("...") and len(line) < 260


# -- validate_skills ---------------------------------------------------------------------------


def test_validate_skills_clean(skills):
    assert sb.validate_skills(skills.values(), ALL_TOOLS, LOCAL_TOOLS) == []


def test_validate_skills_reports_problems(skills, caplog):
    with caplog.at_level(logging.WARNING, logger=sb.logger.name):
        problems = sb.validate_skills(skills.values(), VIEWER_TOOLS, LOCAL_TOOLS)
    assert "skill 'returns-and-refunds': tool `create_return` is not on its declared servers" in problems
    assert "skill 'support-ticketing': tool `create_ticket` is not on its declared servers" in problems
    assert len(caplog.records) == len(problems)


def test_validate_skills_empty_server_and_undeclared_server():
    skill = Skill(name="s", description="d", instructions="Use `get_order`.", metadata={"mcp-servers": "support"})
    problems = sb.validate_skills([skill], ALL_TOOLS, LOCAL_TOOLS)
    assert problems == ["skill 's': tool `get_order` is not on its declared servers "
                        "(exists as orders___get_order, but that server isn't declared)"]
    problems = sb.validate_skills([skill], {}, LOCAL_TOOLS)
    assert "skill 's': declared server 'support' has no tools on the Gateway" in problems


def test_validate_skills_missing_level(skills, caplog):
    with caplog.at_level(logging.INFO, logger=sb.logger.name):
        sb.validate_skills(skills.values(), VIEWER_TOOLS, LOCAL_TOOLS, missing_level=logging.INFO)
    assert caplog.records and all(r.levelno == logging.INFO for r in caplog.records)


def test_skill_binding_server_index(skills):
    binding = sb.SkillBinding(skills.values())
    assert binding._server_skills == {
        "orders": ["order-tracking", "returns-and-refunds"],
        "support": ["returns-and-refunds", "support-ticketing"],
    }
