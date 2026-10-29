"""Local AgentCore Gateway emulator: one MCP endpoint with the Gateway's prefixed tool names.

Serves `orders___*` (orders Lambda tool functions, schemas from tools_schema.json) and
`support___*` (support MCP tool functions) on http://localhost:8000/mcp with STORE=memory.
Like the real Gateway, each tool returns its JSON result as text content.

`--role viewer` mirrors the per-user AgentCore Policy filtering (design.md section 8): only the
read-only tools are listed, and calls to any other tool fail.

Usage: uv run python scripts/local_gateway.py [--role admin|viewer] [--port 8000]

What this file does
    Runs a stand-in for the AWS AgentCore Gateway on your own computer. It offers the same seven
    tools under the same names as the real Gateway, but it runs the tool code directly in this
    program and keeps all data in memory. No AWS account, no Lambda, no DynamoDB.

Where it fits
    Start this first, then start `scripts/run_local.py` in a second terminal to chat with the agent.
    The agent connects here exactly as it would connect to the real Gateway, so you can try the
    skills and tools for free. Use `--role viewer` to see how the agent behaves for a read-only user
    like bob (no `create_return`, no `create_ticket`).

Step by step
    1. Switch the data store to memory mode, loaded fresh from data/seed.json.
    2. Build the tool list: the four orders tools (names and input fields from tools_schema.json,
       the same file the real Gateway uses) and the three support tools (from support_mcp/server.py).
       Each gets the Gateway-style name "<target>___<tool>", e.g. "orders___get_order".
    3. For the viewer role, keep only the five read-only tools.
    4. Serve them as one MCP server on http://localhost:8000/mcp until you press Ctrl+C.

Key terms
    Emulator: a local imitation of a cloud service, for testing.
    MCP (Model Context Protocol): a standard way for an AI agent to discover and call tools.
    AgentCore Policy: the real Gateway's per-user permission rules (written in the Cedar language).
        Here a simple list (VIEWER_TOOLS) imitates them; there is no sign-in locally.
    Tool schema: the description of a tool's inputs (names, types, which are required).
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Make the tool code in mcp_servers/ importable from this script.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp_servers"))

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.tools import Tool  # noqa: E402

from orders_lambda import handler as orders  # noqa: E402
from support_mcp import server as support  # noqa: E402
from common.store import get_store  # noqa: E402

ORDERS_SCHEMA = ROOT / "mcp_servers" / "orders_lambda" / "tools_schema.json"
# The read-only tools a viewer (bob) may use. This mirrors the "viewers" Cedar policy in design.md
# section 8; the two write tools (orders___create_return, support___create_ticket) are left out.
VIEWER_TOOLS = frozenset({
    "orders___get_order",
    "orders___list_customer_orders",
    "orders___check_return_eligibility",
    "support___get_ticket",
    "support___search_faq",
})


def _as_text(fn: Callable[..., dict[str, Any]]) -> Callable[..., str]:
    """Wrap a tool so it returns its JSON as a string (keeps the signature for argument validation).

    The real Gateway hands tool results back as JSON text, so the local copy does the same. This keeps
    the agent's experience identical in both places.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        return json.dumps(fn(*args, **kwargs))

    return wrapper


def gateway_tools() -> list[Tool]:
    """All 7 tools with Gateway names: orders (inline schemas) and support (server schemas)."""
    tools = []
    # Step 1: the four orders tools. Name, description and input schema come from tools_schema.json
    # (what the real Gateway shows the model); the work is done by the Lambda's Python functions.
    for spec in json.loads(ORDERS_SCHEMA.read_text(encoding="utf-8")):
        tool = Tool.from_function(_as_text(orders.TOOLS[spec["name"]]), name=f"orders___{spec['name']}",
                                  description=spec["description"], structured_output=False)
        tools.append(tool.model_copy(update={"parameters": spec["inputSchema"]}))
    # Step 2: the three support tools, described by the support server's own function definitions.
    for fn in support.SUPPORT_TOOLS:
        tools.append(Tool.from_function(_as_text(fn), name=f"support___{fn.__name__}", structured_output=False))
    return tools


def build_server(role: str = "admin") -> MCPServer:
    """Gateway emulator for a role: `admin` gets every tool, `viewer` only the read-only ones."""
    if role not in ("admin", "viewer"):
        raise ValueError(f"unknown role {role!r}")
    # Step 1: use the in-memory store and forget any store made earlier, so the data starts fresh
    # from data/seed.json.
    os.environ["STORE"] = "memory"
    get_store.cache_clear()
    # Step 2: pick the tools for this role. A tool that is not registered simply does not exist for
    # the viewer, so it is neither listed nor callable (like the real Gateway's filtered tools/list).
    tools = [t for t in gateway_tools() if role == "admin" or t.name in VIEWER_TOOLS]
    return MCPServer("shopassist-local-gateway", tools=tools)


def main() -> None:
    """Read the options, build the emulator and serve it until stopped."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--role", choices=["admin", "viewer"], default="admin")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = build_server(args.role)
    print(f"Local gateway ({args.role}) on http://{args.host}:{args.port}/mcp", flush=True)
    # Same transport settings as the deployed support server: MCP over HTTP at /mcp, no session state.
    server.run("streamable-http", host=args.host, port=args.port, streamable_http_path="/mcp", stateless_http=True)


if __name__ == "__main__":
    main()
