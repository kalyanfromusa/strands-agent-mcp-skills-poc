"""Per-user access demo (design.md section 8) against the deployed stack.

    .venv/Scripts/python.exe scripts/demo_access.py              # all scenarios
    .venv/Scripts/python.exe scripts/demo_access.py --only b,d   # a subset
    .venv/Scripts/python.exe scripts/demo_access.py --no-reseed  # don't reset the demo orders first

Scenarios (one agent, one Gateway, three users):
  a  alice (shop-admin) asks the agent to return the 1042 headphones -> orders___create_return is used
  b  bob (shop-viewer) asks the same -> no create_return, the agent explains he lacks permission
  c  carol (no group) -> the Runtime's JWT authorizer rejects her with HTTP 401/403
  d  direct Gateway calls without the agent: bob's tools/list lacks the write tools and his
     tools/call orders___create_return is denied; alice's tools/list shows all 7 tools

Inputs: .deploy/outputs.json (region, agent_runtime_arn, gateway_url, cognito_users_client_id) and
.deploy/users.json. Scenario (a) creates a real return for order 1042, so by default the demo first
re-seeds the orders table (scripts/seed_data.py) to make the item eligible again; bob's scenarios run
before alice's for the same reason. Exits non-zero when any outcome is unexpected.
"""

# What this file does
#   Runs the per-user access demo automatically against the deployed stack and prints a table of
#   pass/fail results. It shows that ONE agent and ONE Gateway give three users three different
#   outcomes, and that the rules are enforced by AWS services outside the agent code.
#   (The help text above, inside the triple quotes, is also what `--help` prints.)
#
# Where it fits
#   Run it after a deployment, to prove the access control works (for example before showing the
#   PoC to someone). It reuses the sign-in and HTTPS call code from scripts/invoke_agent.py and the
#   re-seed code from scripts/seed_data.py. It costs a few model calls on Bedrock.
#
# The four scenarios: what each proves, and which security layer enforces it
#   a  alice (group shop-admin) asks the agent to return the order 1042 headphones.
#      Proves: an allowed user can do a write action. Every layer allows her: the Runtime sign-in
#      check, the Gateway sign-in check and the AgentCore Policy. Passes if the agent used
#      orders___create_return and reported no error.
#   b  bob (group shop-viewer) sends the same message.
#      Proves: a read-only user cannot create a return, and the agent explains why politely.
#      Enforced by AgentCore Policy: the Gateway hides create_return from bob's tool list, so the
#      agent cannot call it. The skill's instructions tell the agent to say the account lacks
#      permission. Passes if create_return was NOT used and the reply talks about permission/access.
#   c  carol (no group) sends the same message.
#      Proves: a user without an allowed group cannot use the agent at all.
#      Enforced by the Runtime's JWT authorizer: it checks the token's "cognito:groups" claim
#      before any agent code runs. Passes if the call is rejected with HTTP 401 or 403.
#   d  Direct calls to the Gateway, without the agent (as if someone tried to bypass it).
#      Proves: the restriction lives in the Gateway, not only in the agent's behaviour.
#      Enforced by AgentCore Policy (Cedar rules) at the Gateway. Three checks:
#        - bob's tool list (tools/list) has the 5 read-only tools and NOT the 2 write tools;
#        - bob's direct call (tools/call) to orders___create_return is refused;
#        - alice's tool list has all 7 tools.
#
# Step by step
#   1. Read the options and check .deploy/outputs.json has everything needed.
#   2. If scenario a or b will run, re-seed the orders table so order 1042 is returnable again
#      (an earlier run of scenario a has already returned it).
#   3. Run the chosen scenarios in the order b, c, d, a (bob before alice, see RUN_ORDER below).
#      An unexpected error in one scenario is recorded as a failed row; the others still run.
#   4. Print the results table and exit with 0 if all rows passed, 1 otherwise.
#
# Key terms
#   JWT authorizer: the Runtime's built-in check of the user's login token (JSON Web Token).
#   Claim: one fact inside the token, e.g. "cognito:groups": ["shop-viewer"].
#   AgentCore Policy / Cedar: per-user permission rules at the Gateway, written in the Cedar language.
#       Anything not explicitly allowed is denied.
#   tools/list and tools/call: the two MCP requests "which tools are there?" and "run this tool".
#   MCP (Model Context Protocol): a standard way for an AI agent to discover and call tools.

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Make the sibling scripts (invoke_agent.py, seed_data.py) importable.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from invoke_agent import (  # noqa: E402
    DEPLOY,
    AccessDenied,
    invoke_https,
    load_json,
    new_session_id,
    runtime_url,
    token_for_user,
)

# The message alice, bob and carol all send in scenarios a, b and c. Order 1042 is the demo order:
# wireless headphones (electronics), delivered 5 days ago, so it is inside the 15-day return window.
RETURN_PROMPT = "I'm customer C001. Please return the headphones from order 1042, they're uncomfortable."
CREATE_RETURN = "orders___create_return"
CREATE_TICKET = "support___create_ticket"
# The two tools that change data. Viewers (bob) must not see or use them.
WRITE_TOOLS = (CREATE_RETURN, CREATE_TICKET)
# All seven tools on the Gateway, with their "<target>___<tool>" names. Admins (alice) see them all.
ALL_TOOLS = (
    "orders___get_order",
    "orders___list_customer_orders",
    "orders___check_return_eligibility",
    CREATE_RETURN,
    CREATE_TICKET,
    "support___get_ticket",
    "support___search_faq",
)
# The arguments for bob's direct create_return call in scenario d (it is expected to be refused).
DIRECT_RETURN_ARGS = {"order_id": "1042", "sku": "EL-HP-100", "reason": "access demo: direct Gateway call"}

SCENARIOS = ("a", "b", "c", "d")
# Bob first: alice's return in (a) makes the item ineligible, which would change bob's conversation.
# So alice (a) goes last: hers is the only scenario expected to create a real return, and every
# scenario before it should see order 1042 as still returnable.
RUN_ORDER = ("b", "c", "d", "a")

# The text for the "enforced by" column of the results table: which security layer each check shows.
WHY_RUNTIME = "Runtime JWT authorizer (cognito:groups claim)"
WHY_POLICY = "AgentCore Policy (Cedar) at the Gateway"
WHY_POLICY_LIST = "AgentCore Policy: tools/list filtered per user"
WHY_SKILL = "AgentCore Policy hides the tool + skill guidance explains"
WHY_ALLOWED = "all layers allow (admin)"

# Words that show the agent's reply explains a missing permission (scenario b).
_PERMISSION = re.compile(
    r"permission|not allowed|isn.t allowed|not authori[sz]ed|unauthori[sz]ed|authori[sz]ation|access|privilege",
    re.IGNORECASE,
)
# Words that show the Gateway's answer to a direct tool call is a refusal (scenario d).
_DENIAL = re.compile(
    r"denied|deny|forbidden|not authori[sz]ed|unauthori[sz]ed|not allowed|permission|policy|access",
    re.IGNORECASE,
)


@dataclass
class Outcome:
    """One row of the results table: which scenario, what was checked, pass or fail, and why."""

    scenario: str
    check: str
    ok: bool
    why: str
    detail: str = ""


# -- pure evaluation (unit-tested) ---------------------------------------------------------------
# The functions below only judge results that are handed to them; they make no network calls, so the
# tests can check every pass/fail rule without AWS.


def parse_only(value: str | None) -> list[str]:
    """`"a, d"` -> `["a", "d"]` in canonical order; None/empty -> all scenarios."""
    if not value:
        return list(SCENARIOS)
    picked = {v.strip().lower() for v in value.split(",") if v.strip()}
    unknown = sorted(picked - set(SCENARIOS))
    if unknown:
        raise ValueError(f"unknown scenario(s): {', '.join(unknown)} (choose from {', '.join(SCENARIOS)})")
    return [s for s in SCENARIOS if s in picked]


def _tools_used(result: dict[str, Any]) -> list[str]:
    """The names of the tools the agent used, from its reply (empty list if none)."""
    return [str(t) for t in (result.get("tools_used") or [])]


def evaluate_admin_return(result: dict[str, Any]) -> Outcome:
    """Scenario a: pass if alice's agent called orders___create_return and reported no error."""
    tools = _tools_used(result)
    ok = CREATE_RETURN in tools and not result.get("error")
    detail = f"tools: {', '.join(tools) or '-'}"
    if result.get("error"):
        detail += f"; agent error: {result['error']}"
    return Outcome("a", "alice: agent creates the return", ok, WHY_ALLOWED, detail)


def evaluate_viewer_return(result: dict[str, Any]) -> Outcome:
    """Scenario b: pass if bob's agent did NOT call create_return and its reply explains the permission."""
    tools = _tools_used(result)
    response = str(result.get("response") or "")
    called = CREATE_RETURN in tools
    explained = bool(_PERMISSION.search(response))
    ok = not called and explained and not result.get("error")
    if called:
        detail = "create_return WAS called"
    elif not explained:
        detail = f"no permission explanation in reply: {response[:120]!r}"
    else:
        detail = f"no create_return; tools: {', '.join(tools) or '-'}"
    return Outcome("b", "bob: no return, permission explained", ok, WHY_SKILL, detail)


def evaluate_runtime_denied(result: dict[str, Any] | None, error: BaseException | None) -> Outcome:
    """Scenario c: pass only if carol's call was refused with HTTP 401 or 403.

    Any other error, or a normal answer from the agent, is a fail.
    """
    check = "carol: agent call rejected (401/403)"
    if isinstance(error, AccessDenied) and error.status in (401, 403):
        return Outcome("c", check, True, WHY_RUNTIME, f"HTTP {error.status}")
    if error is not None:
        return Outcome("c", check, False, WHY_RUNTIME, f"unexpected error: {error}")
    reply = str((result or {}).get("response") or "")
    return Outcome("c", check, False, WHY_RUNTIME, f"agent answered: {reply[:120]!r}")


def evaluate_tool_list(
    user: str, names: Iterable[str], present: Iterable[str], absent: Iterable[str] = ()
) -> Outcome:
    """Scenario d: pass if the Gateway's tool list has every `present` tool and none of the `absent` ones.

    For bob, `absent` is the two write tools (they must be hidden); for alice nothing must be hidden.
    """
    have = set(names)
    missing = sorted(set(present) - have)
    leaked = sorted(set(absent) & have)
    ok = not missing and not leaked
    parts = [f"{len(have)} tools"]
    if missing:
        parts.append(f"missing: {', '.join(missing)}")
    if leaked:
        parts.append(f"should be hidden: {', '.join(leaked)}")
    if absent and not leaked:
        parts.append("write tools hidden")
    why = WHY_POLICY_LIST if absent else WHY_ALLOWED
    return Outcome("d", f"{user}: Gateway tools/list", ok, why, "; ".join(parts))


def call_result_text(result: Any) -> str:
    """Concatenated text content of an MCP CallToolResult (or a dict)."""
    if result is None:
        return ""
    if isinstance(result, dict):
        return json.dumps(result)
    texts = [getattr(block, "text", "") for block in (getattr(result, "content", None) or [])]
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        texts.append(json.dumps(structured, default=str))
    return " ".join(t for t in texts if t)


def is_call_denied(result: Any, error: BaseException | None) -> tuple[bool, str]:
    """Was a direct tools/call refused (rather than executed by the tool)?"""
    # Step 1: an error raised by the call itself (an MCP error answer) counts as refused.
    if error is not None:
        return True, f"{type(error).__name__}: {str(error)[:120]}"
    # Step 2: if the answer contains a return id, the tool really ran: that is NOT a refusal.
    text = call_result_text(result)
    if "return_id" in text or "RET-" in text:
        return False, f"the tool ran: {text[:120]}"
    # Step 3: an answer flagged as an error, or one using refusal words, counts as refused.
    is_error = bool(getattr(result, "is_error", False) or (isinstance(result, dict) and result.get("isError")))
    if is_error:
        return True, f"isError: {text[:120]}"
    if _DENIAL.search(text):
        return True, text[:120]
    return False, f"not denied: {text[:120] or '(empty result)'}"


def evaluate_direct_call(user: str, result: Any, error: BaseException | None) -> Outcome:
    """Scenario d: pass if bob's direct call to orders___create_return was refused by the Gateway."""
    denied, detail = is_call_denied(result, error)
    return Outcome("d", f"{user}: Gateway tools/call {CREATE_RETURN} denied", denied, WHY_POLICY, detail)


def render_table(outcomes: list[Outcome]) -> str:
    """Format the results as an aligned text table, sorted by scenario (a, b, c, d)."""
    rows = [("", "#", "check", "enforced by", "detail")]
    for o in sorted(outcomes, key=lambda o: SCENARIOS.index(o.scenario) if o.scenario in SCENARIOS else 99):
        rows.append(("✅" if o.ok else "❌", o.scenario, o.check, o.why, o.detail))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    lines = []
    for r in rows:
        lines.append("  ".join(r[i].ljust(widths[i]) for i in range(4)) + "  " + r[4])
    lines.insert(1, "-" * (sum(widths) + 2 * 4 + 6))
    return "\n".join(lines)


def exit_code(outcomes: list[Outcome]) -> int:
    """0 if there were results and all of them passed, otherwise 1 (so automation can spot a failure)."""
    return 0 if outcomes and all(o.ok for o in outcomes) else 1


# -- live scenarios ------------------------------------------------------------------------------
# The code below talks to the deployed agent and Gateway.


class Demo:
    """Runs the live scenarios against the deployed agent and Gateway, one method per scenario."""

    def __init__(self, outputs: dict[str, Any], users_path: Path, timeout: float = 300) -> None:
        self.outputs = outputs
        self.users_path = users_path
        self.timeout = timeout
        self.url = runtime_url(outputs["region"], outputs["agent_runtime_arn"])
        self._tokens: dict[str, str] = {}

    def token(self, user: str) -> str:
        """The user's Cognito access token (signed in once per user, then reused)."""
        if user not in self._tokens:
            self._tokens[user] = token_for_user(user, self.outputs, self.users_path)
        return self._tokens[user]

    def ask(self, user: str, prompt: str) -> dict[str, Any]:
        """Send one message to the agent as this user, in a brand-new session (no earlier conversation)."""
        return invoke_https(self.url, self.token(user), new_session_id(), prompt, timeout=self.timeout)

    def scenario_a(self) -> list[Outcome]:
        """alice (admin) asks for the return; the agent should create it."""
        return [evaluate_admin_return(self.ask("alice", RETURN_PROMPT))]

    def scenario_b(self) -> list[Outcome]:
        """bob (viewer) asks for the same return; the agent should explain he lacks permission."""
        return [evaluate_viewer_return(self.ask("bob", RETURN_PROMPT))]

    def scenario_c(self) -> list[Outcome]:
        """carol (no group) tries to use the agent; the Runtime should reject her with 401/403."""
        # Step 1: sign in. carol is a real Cognito user, so this should work; the refusal comes later.
        try:
            token = self.token("carol")
        except SystemExit as e:  # Cognito refused the sign-in itself
            return [Outcome("c", "carol: agent call rejected (401/403)", False, WHY_RUNTIME, f"sign-in failed: {e}")]
        # Step 2: call the agent. An AccessDenied (401/403) error here is the expected result.
        try:
            result = invoke_https(self.url, token, new_session_id(), RETURN_PROMPT, timeout=self.timeout)
        except Exception as e:  # noqa: BLE001
            return [evaluate_runtime_denied(None, e)]
        return [evaluate_runtime_denied(result, None)]

    def scenario_d(self) -> list[Outcome]:
        """Talk to the Gateway directly (no agent) with bob's and then alice's token."""
        # Step 1: as bob, list the tools and try create_return. Step 2: as alice, list the tools.
        # Step 3: judge the three results.
        url = self.outputs["gateway_url"]
        bob_tools, call_result, call_error = asyncio.run(gateway_viewer_checks(url, self.token("bob")))
        alice_tools = asyncio.run(gateway_list_tools(url, self.token("alice")))
        return [
            evaluate_tool_list("bob", bob_tools, set(ALL_TOOLS) - set(WRITE_TOOLS), WRITE_TOOLS),
            evaluate_direct_call("bob", call_result, call_error),
            evaluate_tool_list("alice", alice_tools, ALL_TOOLS),
        ]


async def _with_gateway(url: str, token: str, fn: Callable[[Any], Any]) -> Any:
    """Open an MCP connection to the Gateway with this user's token, run `fn` on it, then close it."""
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with Client(streamable_http_client(url, http_client=http), mode="legacy", cache=None) as client:
            return await fn(client)


async def _all_tool_names(client: Any) -> list[str]:
    """Ask the Gateway for its tool list (MCP tools/list), following the pages until the last one."""
    names: list[str] = []
    cursor: str | None = None
    while True:
        page = await client.list_tools(cursor=cursor)
        names.extend(t.name for t in page.tools)
        cursor = page.next_cursor
        if not cursor:
            return names


async def gateway_list_tools(url: str, token: str) -> list[str]:
    """The names of the tools the Gateway shows to this user."""
    return await _with_gateway(url, token, _all_tool_names)


async def gateway_viewer_checks(url: str, token: str) -> tuple[list[str], Any, BaseException | None]:
    """For bob: get his tool list, then try orders___create_return directly (MCP tools/call).

    Returns the tool names, the call's result (if any) and the call's error (if any).
    """
    async def run(client: Any) -> tuple[list[str], Any, BaseException | None]:
        names = await _all_tool_names(client)
        try:
            return names, await client.call_tool(CREATE_RETURN, DIRECT_RETURN_ARGS), None
        except Exception as e:  # noqa: BLE001  (a JSON-RPC error is a denial)
            return names, None, e

    return await _with_gateway(url, token, run)


def reseed(region: str) -> None:
    """Reset the demo orders in DynamoDB (see scripts/seed_data.py) so order 1042 is returnable again."""
    from seed_data import seed

    print(f"re-seeding demo orders in {region} ... {seed(region)} orders")


def main() -> int:
    """Run the selected scenarios and print the results table. Returns 0 if all passed, 1 otherwise."""
    # Step 1: read the options.
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="comma-separated subset of a,b,c,d (default: all)")
    parser.add_argument("--outputs", default=str(DEPLOY / "outputs.json"))
    parser.add_argument("--users", default=str(DEPLOY / "users.json"))
    parser.add_argument("--no-reseed", action="store_true", help="don't re-seed the orders table before (a)/(b)")
    parser.add_argument("--timeout", type=float, default=300, help="per agent call, seconds")
    args = parser.parse_args()
    try:
        selected = parse_only(args.only)
    except ValueError as e:
        parser.error(str(e))

    # Step 1 (continued): the deployment outputs must name the region, the agent, the Gateway and the
    # Cognito users app client.
    outputs = load_json(Path(args.outputs))
    for key in ("region", "agent_runtime_arn", "gateway_url", "cognito_users_client_id"):
        if not outputs.get(key):
            raise SystemExit(f"{args.outputs} has no {key!r}")
    demo = Demo(outputs, Path(args.users), timeout=args.timeout)
    # Step 2: re-seed only when a scenario that depends on order 1042 will run.
    if not args.no_reseed and {"a", "b"} & set(selected):
        reseed(outputs["region"])

    # Step 3: run the scenarios in RUN_ORDER. An error becomes a failed row instead of stopping the demo.
    outcomes: list[Outcome] = []
    for name in (s for s in RUN_ORDER if s in selected):
        print(f"running scenario {name} ...", flush=True)
        try:
            outcomes.extend(getattr(demo, f"scenario_{name}")())
        except Exception as e:  # noqa: BLE001
            outcomes.append(Outcome(name, f"scenario {name}", False, "-", f"{type(e).__name__}: {e}"))

    # Step 4: show the table and set the exit code.
    print()
    print(render_table(outcomes))
    code = exit_code(outcomes)
    print("\nAll outcomes as expected." if code == 0 else "\nSome outcomes were unexpected (see ❌ rows).")
    return code


# The results table uses check-mark symbols; switch the console to UTF-8 so Windows can print them.
if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # ✅/❌ on Windows consoles
    sys.exit(main())
