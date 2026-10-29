"""Hybrid skill-to-tool binding.

What this file does
    Connects skills to the Gateway tools they are allowed to use, and blocks everything else. It also records
    which skills and tools each request used, so the command-line scripts can print them.

Where it fits
    `app.py` adds `SkillBinding` to the agent as a Strands "plugin". Strands then calls its hooks at fixed
    moments: before each request, before each tool call and after each tool call. `app.py` also calls
    `validate_skills()` once after building the agent, to log mistakes in the skill files.

How a skill is tied to tools (the "hybrid" binding)
    A skill declares the MCP servers it uses in its frontmatter
    (`metadata: { mcp-servers: "<server> <server>" }`) and refers to tools by their plain
    names in its instructions. Gateway tools are named `<server>___<tool>`. This module:

    (a) appends the resolved Gateway tool list for the declared servers to the result of the
        `skills` activation tool, and marks declared tools the current caller doesn't have;
    (b) blocks a Gateway tool call unless an activated skill declares its server;
    (c) validates skills against the Gateway tool list at startup.

    Example: the `returns-and-refunds` skill declares `mcp-servers: "orders support"` and its text mentions
    `check_return_eligibility`. The Gateway names that tool `orders___check_return_eligibility` (target name,
    three underscores, tool name). When the model activates the skill, this module appends the exact names,
    for example "- orders___check_return_eligibility — ...". If the current user (bob) is not allowed
    `create_return`, the Gateway hides it, and the appended text says
    "Not available to the current user: create_return" so the model explains instead of trying anyway.

    Why "hybrid": skill authors write simple tool names and list only which servers they need, while the agent
    works out the exact names at run time from what the Gateway actually offers this user.

Step by step (during one request)
    1. Before the request: reset the "skills used" and "tools used" lists.
    2. The model activates a skill with the `skills` tool. After that call, append the exact tool names for the
       skill's declared servers (and what is missing for this user) to the skill text the model receives.
    3. Before every tool call: if it is a Gateway tool whose server no activated skill declares, cancel the
       call and send the model a message explaining what to do instead. If the model used a plain name such
       as `get_order`, tell it the exact Gateway name.
    4. After every tool call: record the tool, and credit the call to the skill that declared its server.

    It also records which skills and tools each invocation used. It contains no knowledge of
    any particular domain: everything is derived from skill metadata and the tool list.

Key terms
    MCP (Model Context Protocol) server: a group of tools. Here there are two Gateway targets: `orders`
        (a Lambda function) and `support` (a FastMCP server on AgentCore Runtime).
    Frontmatter: the small settings block between `---` lines at the top of a SKILL.md file.
    Plugin / hook: a plugin adds behaviour to the Strands agent; a hook is a function Strands calls when a
        certain event happens (for example "a tool is about to be called").
    Tool registry: the agent's list of every tool it can call, by name.
    Activated skill: a skill whose full instructions the model has loaded in this conversation.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any

from strands.hooks import AfterToolCallEvent, BeforeInvocationEvent, BeforeToolCallEvent
from strands.plugins import Plugin, hook
from strands.vended_plugins.skills import Skill

logger = logging.getLogger(__name__)

# The Gateway names each tool "<target>___<tool>" (three underscores), e.g. "orders___get_order".
GATEWAY_SEP = "___"
# Name of the tool the AgentSkills plugin provides for activating a skill.
SKILLS_TOOL = "skills"
# Where the AgentSkills plugin keeps its state (including "activated_skills") in agent.state.
SKILLS_STATE_KEY = "agent_skills"
# The frontmatter metadata field in which a skill lists the MCP servers it needs.
SERVERS_KEY = "mcp-servers"

# Backticked snake_case identifiers with at least one underscore, e.g. `get_thing`.
_TOOL_REF = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")


def split_gateway_name(name: str) -> tuple[str, str] | None:
    """`server___tool` -> (`server`, `tool`); None for non-Gateway names.

    Local tools such as `skills` and `read_skill_resource` have no "___", so they return None.
    """
    server, sep, plain = name.partition(GATEWAY_SEP)
    if not sep or not server or not plain:
        return None
    return server, plain


def declared_servers(skill: Skill) -> list[str]:
    """The MCP servers a skill lists in `metadata.mcp-servers`, in order and without repeats.

    Accepts a space-separated text ("orders support") or a list. A skill with no entry uses no Gateway tools.
    """
    raw = (skill.metadata or {}).get(SERVERS_KEY)
    if raw is None:
        return []
    items = raw.split() if isinstance(raw, str) else [str(x) for x in raw]
    seen: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen


def plain_tool_refs(instructions: str, ignore: Iterable[str] = ()) -> list[str]:
    """Plain tool names referenced in backticks in a skill body (ordered, unique).

    For example `get_order` in the text gives "get_order". Names in `ignore` (the local tools such as
    `read_skill_resource`) are left out, because they don't come from the Gateway.
    """
    skip = set(ignore)
    out: list[str] = []
    for name in _TOOL_REF.findall(instructions or ""):
        if name not in skip and name not in out:
            out.append(name)
    return out


def _short(desc: str, limit: int = 200) -> str:
    """Squash a tool description onto one line and cut it to `limit` characters, to keep the prompt small."""
    line = " ".join((desc or "").split())
    return line if len(line) <= limit else line[: limit - 3] + "..."


def server_tools(gateway_tools: Mapping[str, str], server: str) -> dict[str, str]:
    """The Gateway tools (name -> description) that belong to one server, e.g. all `orders___...` tools."""
    prefix = server + GATEWAY_SEP
    return {name: desc for name, desc in gateway_tools.items() if name.startswith(prefix)}


def missing_declared_tools(skill: Skill, gateway_tools: Mapping[str, str], local_tools: Iterable[str] = ()) -> list[str]:
    """Plain tool names the skill mentions that no declared server provides.

    `gateway_tools` is the list this caller can see. The Gateway hides tools the user may not call, so for
    bob this returns, for example, ["create_return"]. For alice it is normally empty.
    """
    servers = declared_servers(skill)
    available_plain = {
        parts[1] for name in gateway_tools if (parts := split_gateway_name(name)) and parts[0] in servers
    }
    return [ref for ref in plain_tool_refs(skill.instructions, ignore=local_tools) if ref not in available_plain]


def resolution_text(skill: Skill, gateway_tools: Mapping[str, str], local_tools: Iterable[str] = ()) -> str:
    """The text appended to a skill activation: exact tool names for the declared servers.

    This is the "[Tool binding]" block the model reads right after the skill's own instructions. It turns the
    skill's plain names into exact Gateway names, and lists what the current user can't use, with an
    instruction to explain that politely instead of working around it.
    """
    # Step 1: a skill that declares no servers gets a one-line note.
    servers = declared_servers(skill)
    if not servers:
        return "[Tool binding] This skill declares no MCP servers, so it uses no Gateway tools."
    lines = ["[Tool binding] Call this skill's tools by these exact names:"]
    empty: list[str] = []
    # Step 2: list each declared server's tools by exact name with a short description. Remember the servers
    # that offer this user no tools at all.
    for server in servers:
        tools = server_tools(gateway_tools, server)
        if not tools:
            empty.append(server)
        for name in sorted(tools):
            lines.append(f"- {name} — {_short(tools[name])}")
    if len(lines) == 1:
        lines.append("- (none)")
    # Step 3: name the tools the skill mentions that this user doesn't have (hidden by the Gateway policy).
    missing = missing_declared_tools(skill, gateway_tools, local_tools)
    if missing:
        lines.append(f"Not available to the current user: {', '.join(missing)}")
    if empty:
        lines.append(f"No tools available to the current user on server(s): {', '.join(empty)}")
    # Step 4: if anything is missing, tell the model how to behave: explain the permission limit, don't improvise.
    if missing or empty:
        lines.append(
            "If a step needs an unavailable tool, tell the user their account doesn't have permission for that "
            "action and offer what you can do instead. Do not try to work around it."
        )
    return "\n".join(lines)


def decide_tool_call(
    tool_name: str,
    active_servers: Iterable[str],
    server_skills: Mapping[str, list[str]],
    known_tools: Iterable[str] = (),
) -> str | None:
    """Return a cancel message if the call must be blocked, else None.

    - Gateway tools (`server___tool`) need an activated skill that declares `server`.
    - Unknown plain names that match a Gateway tool get a hint with the exact name.
    - Everything else (e.g. `skills`, `read_skill_resource`) is allowed.

    `active_servers` are the servers declared by the skills activated so far. `server_skills` maps each server
    to the skills that declare it (used to suggest which skill to activate). `known_tools` is every tool name
    the agent has. This is the agent-side guard; the Gateway policy is a second, independent check.
    """
    active = set(active_servers)
    known = set(known_tools)
    parts = split_gateway_name(tool_name)
    # Case 1: a name without "___" (a local tool, or a plain name the model guessed).
    if parts is None:
        if tool_name in known:
            return None
        # The model may have called "get_order" instead of "orders___get_order". Find the real name(s).
        matches = sorted(n for n in known if (p := split_gateway_name(n)) and p[1] == tool_name)
        if matches:
            usable = [n for n in matches if split_gateway_name(n)[0] in active]  # type: ignore[index]
            if usable:
                return f"Unknown tool '{tool_name}'. Use the exact name: {', '.join(usable)}."
            return (
                f"Unknown tool '{tool_name}'. Its Gateway name is {', '.join(matches)}, which needs an "
                "activated skill that declares its server. Activate the right skill with the `skills` tool first."
            )
        # Not a Gateway tool name at all: let Strands handle it as usual.
        return None
    # Case 2: a Gateway tool. Allow it only if an activated skill declares its server.
    server, _ = parts
    if server in active:
        return None
    # Blocked: suggest the skills that would unlock this server, if any.
    candidates = server_skills.get(server, [])
    if candidates:
        hint = f"Activate a skill that declares it with the `skills` tool first (candidates: {', '.join(candidates)})."
    else:
        hint = "No available skill declares this server, so this tool can't be used for the current request."
    return f"Tool '{tool_name}' is blocked: no activated skill declares the MCP server '{server}'. {hint}"


def validate_skills(
    skills: Iterable[Skill],
    gateway_tools: Mapping[str, str],
    local_tools: Iterable[str] = (),
    missing_level: int = logging.WARNING,
) -> list[str]:
    """Log (and return) problems: declared servers without tools, mentioned tools that don't exist.

    Run once when the agent is built, to catch mistakes in skill files early (for example a typo in a tool
    name, or a tool that lives on a server the skill forgot to declare). It only logs; it never stops the
    agent. `missing_level` sets how loud the log is (in passthrough mode some tools are expected to be
    missing for some users, so `app.py` logs them as information rather than warnings).
    """
    local = list(local_tools)
    problems: list[str] = []
    for skill in skills:
        for server in declared_servers(skill):
            if not server_tools(gateway_tools, server):
                problems.append(f"skill '{skill.name}': declared server '{server}' has no tools on the Gateway")
        for ref in missing_declared_tools(skill, gateway_tools, local):
            elsewhere = sorted(n for n in gateway_tools if (p := split_gateway_name(n)) and p[1] == ref)
            note = f" (exists as {', '.join(elsewhere)}, but that server isn't declared)" if elsewhere else ""
            problems.append(f"skill '{skill.name}': tool `{ref}` is not on its declared servers{note}")
    for msg in problems:
        logger.log(missing_level, msg)
    return problems


def activated_skill_names(agent: Any) -> list[str]:
    """Names of the skills the model has activated so far, in activation order (from the plugin's state)."""
    state = agent.state.get(SKILLS_STATE_KEY) if agent is not None else None
    if isinstance(state, dict):
        return list(state.get("activated_skills", []))
    return []


def gateway_tools_of(agent: Any) -> dict[str, str]:
    """`{name: description}` for every Gateway tool registered on the agent.

    The agent's tool list was filled from the Gateway's `tools/list` answer for the current user, so this is
    "the Gateway tools this user may use".
    """
    out: dict[str, str] = {}
    for name, tool in agent.tool_registry.registry.items():
        if split_gateway_name(name):
            out[name] = tool.tool_spec.get("description", "")
    return out


class SkillBinding(Plugin):
    """Strands plugin implementing the hybrid binding plus per-invocation usage tracking.

    `skills_used` and `tools_used` are read by `app.py` after each request and returned to the caller, so the
    CLI can print the dim "skills / tools used" line.
    """

    name = "skill_binding"

    def __init__(self, skills: Iterable[Skill]) -> None:
        """Index the skills by name, and build a server -> [skills that declare it] lookup for hints."""
        self._skills = {s.name: s for s in skills}
        self._server_skills: dict[str, list[str]] = {}
        for skill in self._skills.values():
            for server in declared_servers(skill):
                self._server_skills.setdefault(server, []).append(skill.name)
        self.skills_used: list[str] = []
        self.tools_used: list[str] = []
        super().__init__()

    # -- helpers -----------------------------------------------------------------------------
    def active_servers(self, agent: Any) -> set[str]:
        """All servers declared by the skills activated so far: the servers whose tools are unlocked."""
        servers: set[str] = set()
        for name in activated_skill_names(agent):
            skill = self._skills.get(name)
            if skill:
                servers.update(declared_servers(skill))
        return servers

    @staticmethod
    def _local_tools(agent: Any) -> list[str]:
        """Names of the agent's own tools (those without "___"), such as `skills` and `read_skill_resource`."""
        return [n for n in agent.tool_registry.registry if not split_gateway_name(n)]

    def _note_skill(self, name: str) -> None:
        """Add a skill to `skills_used` once."""
        if name not in self.skills_used:
            self.skills_used.append(name)

    # -- hooks -------------------------------------------------------------------------------
    @hook
    def _reset_usage(self, event: BeforeInvocationEvent) -> None:
        """Hook: before each request, start with empty usage lists (they describe this request only)."""
        self.skills_used = []
        self.tools_used = []

    @hook
    def _guard_tool_call(self, event: BeforeToolCallEvent) -> None:
        """Hook: before each tool call, block it if no activated skill allows it.

        Setting `event.cancel_tool` to a message makes Strands skip the tool and give the model that message
        as the tool result, so the model can correct itself (for example activate the right skill first).
        """
        name = event.tool_use["name"]
        message = decide_tool_call(
            name,
            self.active_servers(event.agent),
            self._server_skills,
            known_tools=event.agent.tool_registry.registry.keys(),
        )
        if message:
            logger.info("blocked tool call %s", name)
            event.cancel_tool = message

    @hook
    def _after_tool_call(self, event: AfterToolCallEvent) -> None:
        """Hook: after each tool call, record usage and, for skill activations, append the tool binding."""
        # Step 1: a call we blocked never ran, so don't record it.
        if event.cancel_message:
            return
        name = event.tool_use["name"]
        agent = event.agent
        # Step 2: the model just activated a skill. Append the "[Tool binding]" text to the skill instructions
        # the model receives, with the exact tool names this user can use.
        if name == SKILLS_TOOL:
            skill_name = (event.tool_use.get("input") or {}).get("skill_name")
            skill = self._skills.get(skill_name) if isinstance(skill_name, str) else None
            if skill is None or event.result.get("status") != "success":
                return
            self._note_skill(skill.name)
            text = resolution_text(skill, gateway_tools_of(agent), self._local_tools(agent))
            content = list(event.result.get("content") or [])
            content.append({"text": "\n\n" + text})
            event.result = {**event.result, "content": content}
            return
        # Step 3: any other tool. Record it once in `tools_used`.
        if name not in self.tools_used:
            self.tools_used.append(name)
        parts = split_gateway_name(name)
        if parts:
            # Attribute the call to the most recently activated skill that declares the server.
            for skill_name in reversed(activated_skill_names(agent)):
                skill = self._skills.get(skill_name)
                if skill and parts[0] in declared_servers(skill):
                    self._note_skill(skill_name)
                    break
