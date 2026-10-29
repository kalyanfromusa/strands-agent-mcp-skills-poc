"""Orders tools for the AgentCore Gateway Lambda target `orders`.

The Gateway invokes the Lambda with the tool arguments as the event and the tool name in
`context.client_context.custom["bedrockAgentCoreToolName"]` (`<target>___<tool>`).

What this file does
    It is the code of the AWS Lambda function behind the four "orders" tools the agent can use:
    `get_order`, `list_customer_orders`, `check_return_eligibility` and `create_return`.

Where it fits
    The agent never calls this Lambda directly. The agent asks the AgentCore Gateway to run a tool
    such as `orders___get_order`. The Gateway checks the user's permissions, then invokes this Lambda.
    The tool names, descriptions and input fields the agent sees come from `tools_schema.json`, which
    the Gateway publishes; this file only does the work. It reads and writes data through
    `common/store.py` (DynamoDB when deployed) and applies the return rules in `common/rules.py`.

Step by step (one tool call)
    1. The Gateway invokes `lambda_handler` with two things: the tool's arguments as the "event"
       (e.g. {"order_id": "1042"}), and the full tool name in the call's context
       (e.g. "orders___get_order"; "orders" is the Gateway target name, then three underscores).
    2. `tool_name_from_context` strips the "orders___" prefix, leaving "get_order".
    3. `lambda_handler` looks the name up in TOOLS and checks the arguments match the function.
    4. The tool function runs and returns a dict; the Gateway sends it back to the agent as JSON text.
    Problems come back as a dict with an "error" field (ORDER_NOT_FOUND, NOT_ELIGIBLE, UNKNOWN_TOOL,
    INVALID_INPUT) rather than a crash, so the AI model can read the error and explain it.

Key terms
    AWS Lambda: a service that runs a piece of code on demand, without managing a server.
    AgentCore Gateway: the AWS service that gives the agent one MCP address for many tools and checks
        who may call which tool.
    MCP (Model Context Protocol): a standard way for an AI agent to discover and call tools.
    Gateway target: one tool source behind the Gateway; here "orders" (this Lambda) and "support".
    SKU: the product code of an order line.
"""

from __future__ import annotations

import inspect
import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from common.rules import check_eligibility, find_item
from common.store import get_store

# Log lines go to Amazon CloudWatch Logs when this runs in AWS, which helps when tracing a tool call.
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Address of the (fictional) printable return-shipping label for a return.
LABEL_URL = "https://returns.shopassist.example.com/labels/{return_id}.pdf"


def get_order(order_id: str) -> dict[str, Any]:
    """Full order details, or ORDER_NOT_FOUND.

    Used when a customer asks "where is my order?". The order includes status, carrier, tracking
    number and items.
    """
    order = get_store().get_order(str(order_id).strip())
    return order if order else {"error": "ORDER_NOT_FOUND", "order_id": order_id}


def list_customer_orders(customer_id: str) -> dict[str, Any]:
    """Order summaries for a customer, newest first.

    Only a short summary per order (id, status, date, total), so the agent can let the customer pick
    one before it fetches the full details with `get_order`.
    """
    # Customer ids are stored in upper case ("C001"), so "c001" from the model still matches.
    customer_id = str(customer_id).strip().upper()
    orders = sorted(get_store().list_customer_orders(customer_id), key=lambda o: o["order_date"], reverse=True)
    summaries = [{k: o.get(k) for k in ("order_id", "status", "order_date", "total")} for o in orders]
    return {"customer_id": customer_id, "orders": summaries}


def check_return_eligibility(order_id: str, sku: str) -> dict[str, Any]:
    """Apply the return policy to one item of an order.

    Read-only: it answers "can this item be returned, and for how much?" without changing anything.
    The rules themselves live in `common/rules.py`.
    """
    return check_eligibility(get_store().get_order(str(order_id).strip()), sku)


def create_return(order_id: str, sku: str, reason: str) -> dict[str, Any]:
    """Create a return after re-checking eligibility server-side.

    "Server-side" means this function checks the rules again itself, even if the agent already
    called `check_return_eligibility`. The AI model can make mistakes, so the rule is enforced here,
    where the model cannot skip it.
    """
    store = get_store()
    # Step 1: load the order and apply the return policy.
    order = store.get_order(str(order_id).strip())
    verdict = check_eligibility(order, sku)
    if not verdict["eligible"]:
        return {"error": "NOT_ELIGIBLE", "reason": verdict["reason"]}
    # (If the item is eligible, the order and the item are known to exist.)
    assert order is not None
    item = find_item(order, sku)
    assert item is not None
    # Step 2: build the return record with a random id such as "RET-4F9A1C" (6 hex characters).
    return_id = f"RET-{secrets.token_hex(3).upper()}"
    record = {
        "return_id": return_id,
        "order_id": order["order_id"],
        "customer_id": order["customer_id"],
        "sku": item["sku"],
        "reason": reason,
        "status": "REQUESTED",
        "refund_amount": verdict["refund_amount"],
        "label_url": LABEL_URL.format(return_id=return_id),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    # Step 3: save the return, then flag the item as returned so it cannot be returned twice.
    store.put_return(record)
    store.mark_item_returned(order["order_id"], item["sku"])
    # Step 4: tell the agent only what the customer needs: id, status, refund and label link.
    return {k: record[k] for k in ("return_id", "status", "refund_amount", "label_url")}


# The menu of tools this Lambda offers: plain tool name -> the function that does the work.
# The names must match the names in tools_schema.json.
TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    "get_order": get_order,
    "list_customer_orders": list_customer_orders,
    "check_return_eligibility": check_return_eligibility,
    "create_return": create_return,
}


def tool_name_from_context(context: Any) -> str:
    """Read the Gateway tool name and strip the `<target>___` prefix.

    Example: "orders___create_return" -> "create_return". The Gateway puts the target name in front
    so that tools from different targets never clash; this Lambda only needs the part after it.
    If the name is missing (e.g. a manual test call), an empty name is returned.
    """
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    name = str(custom.get("bedrockAgentCoreToolName", ""))
    return name.split("___", 1)[1] if "___" in name else name


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    """Route a Gateway tool invocation to the matching tool function.

    AWS Lambda calls this function for every request (it is the "entry point" named in the
    Terraform and boto3 deployment).
    """
    # Step 1: work out which tool was asked for, and get its arguments.
    tool = tool_name_from_context(context)
    args = event or {}
    # Log the tool and the argument names only (not their values), to keep customer data out of logs.
    logger.info("tool=%s args=%s", tool, sorted(args))
    # Step 2: find the matching function.
    fn = TOOLS.get(tool)
    if fn is None:
        return {"error": "UNKNOWN_TOOL", "tool": tool}
    # Step 3: check the arguments fit the function (nothing missing, nothing extra) before running it,
    # so a bad call returns a readable error instead of crashing.
    try:
        inspect.signature(fn).bind(**args)
    except TypeError as exc:  # missing or unexpected arguments
        return {"error": "INVALID_INPUT", "message": str(exc)}
    # Step 4: run the tool and return its result to the Gateway.
    return fn(**args)
