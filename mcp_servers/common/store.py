"""Data access for orders, returns and tickets.

`get_store()` returns a `MemoryStore` (seeded from `data/seed.json`) when `STORE=memory`,
otherwise a `DynamoStore` on the tables named by `ORDERS_TABLE`, `RETURNS_TABLE`, `TICKETS_TABLE`.
Both return plain JSON-serialisable dicts (DynamoDB Decimals become int/float).

What this file does
    It is the one place that reads and writes the shop's data: orders, returns and support tickets.
    The tools never talk to a database directly; they ask the "store" for what they need.

Where it fits
    Used by the orders Lambda (`orders_lambda/handler.py`) and by the support MCP server
    (`support_mcp/server.py`). Both container images copy this folder in (see their Dockerfiles), and
    `scripts/seed_data.py` uses `load_seed_orders` to fill the DynamoDB orders table.

    Why two stores? `MemoryStore` keeps everything in the computer's memory, filled from
    `data/seed.json`. It needs no AWS account, so tests and the local demo (`scripts/local_gateway.py`)
    use it; its data disappears when the program stops. `DynamoStore` uses the real DynamoDB tables in
    AWS, so data survives restarts and is shared by every Lambda and Runtime copy. Both offer exactly
    the same operations (the `Store` list below), so the tools work unchanged with either one.

The data model
    Order: order_id, customer_id, status (PROCESSING, SHIPPED, DELIVERED or CANCELLED), order_date,
        delivered_date (empty if not delivered), carrier, tracking_number, total, and items. Each item
        has sku, name, category, qty, price and returned (true once a return was created for it).
    Return: return_id ("RET-xxxxxx"), order_id, customer_id, sku, reason, status, refund_amount,
        label_url, created_at. Saved in the returns table by the `create_return` tool.
    Ticket: ticket_id ("TCK-xxxxxx"), customer_id, order_id, subject, description, priority, status,
        created_at. Saved in the tickets table by the `create_ticket` tool.

    Relative dates: `data/seed.json` does not store real dates. It says, for example, "delivered 12 days
    ago" (`delivered_days_ago: 12`). `resolve_seed_order` turns that into a real date counted back from
    today, so the return-window examples (in window, out of window) stay true whenever the demo runs.

Step by step (typical use)
    1. A tool calls `get_store()`. The first call picks the store type from the STORE setting and
       keeps it for later calls.
    2. The tool calls an operation such as `get_order("1042")` or `put_ticket({...})`.
    3. The store reads or writes memory or DynamoDB and hands back plain Python data (dicts and lists)
       that can be turned into JSON for the AI agent.

Key terms
    DynamoDB: Amazon's managed key-value database. Each table row ("item") is found by its key,
        e.g. order_id.
    GSI (global secondary index): an extra lookup path on a DynamoDB table. Here `customer_id-index`
        lets us find all orders of one customer without reading the whole table.
    Decimal: the exact-number type DynamoDB uses instead of normal floating-point numbers.
    Environment variable: a named setting given to a program when it starts (e.g. STORE=memory).
    Protocol: a Python "contract" listing the methods a class must have; here, what every store offers.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from common.rules import find_item, today_utc

# Default location of the demo data: <repo>/data/seed.json (two folders above this file's folder).
DEFAULT_SEED_FILE = Path(__file__).resolve().parents[2] / "data" / "seed.json"


def to_plain(value: Any) -> Any:
    """Recursively convert DynamoDB Decimals to int/float.

    DynamoDB returns numbers as Decimal, which cannot be turned into JSON directly. Whole numbers
    (like qty 2) become int; others (like price 89.9) become float.
    """
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_plain(v) for v in value]
    return value


def to_dynamo(value: Any) -> Any:
    """Recursively convert floats to Decimal for DynamoDB writes.

    DynamoDB refuses normal floats, so we round-trip the data through JSON and read every decimal
    number back as a Decimal.
    """
    return json.loads(json.dumps(value), parse_float=Decimal)


def resolve_seed_order(raw: dict[str, Any]) -> dict[str, Any]:
    """Turn a seed order (relative `*_days_ago` dates) into a contract order object.

    Example: on 2026-10-29, `"delivered_days_ago": 5` becomes `"delivered_date": "2026-10-24"`.
    """
    today = today_utc()
    # Step 1: copy the order but drop the relative-date fields and the notes for humans
    # (fields starting with "_", such as "_case", which explains what each demo order is for).
    order = {k: v for k, v in raw.items()
             if k not in ("order_days_ago", "delivered_days_ago") and not k.startswith("_")}
    # Step 2: turn "N days ago" into real ISO dates. Orders that were never delivered keep
    # delivered_date empty (None).
    order["order_date"] = (today - timedelta(days=raw["order_days_ago"])).isoformat()
    delivered = raw.get("delivered_days_ago")
    order["delivered_date"] = (today - timedelta(days=delivered)).isoformat() if delivered is not None else None
    # Step 3: every item starts as "not returned" unless the seed file says otherwise
    # (order 1007 has one item already returned).
    order["items"] = [{"returned": False, **item} for item in raw["items"]]
    # Step 4: the order total is always computed from the items (price x qty), never typed in by hand.
    order["total"] = round(sum(i["price"] * i["qty"] for i in order["items"]), 2)
    return order


def load_seed_orders(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Load and resolve every order in the seed file (`SEED_FILE` env overrides the default)."""
    path = Path(path or os.environ.get("SEED_FILE") or DEFAULT_SEED_FILE)
    data = json.loads(path.read_text(encoding="utf-8"))
    return [resolve_seed_order(o) for o in data["orders"]]


class Store(Protocol):
    """Operations the tools need.

    This is the shared contract: both MemoryStore and DynamoStore provide these six operations,
    so the tools can use either one without knowing which.
    """

    def get_order(self, order_id: str) -> dict[str, Any] | None: ...
    def list_customer_orders(self, customer_id: str) -> list[dict[str, Any]]: ...
    def mark_item_returned(self, order_id: str, sku: str) -> None: ...
    def put_return(self, record: dict[str, Any]) -> None: ...
    def put_ticket(self, ticket: dict[str, Any]) -> None: ...
    def get_ticket(self, ticket_id: str) -> dict[str, Any] | None: ...


class MemoryStore:
    """In-process store for local dev and tests.

    Keeps three dictionaries (orders, returns, tickets) in memory. Nothing is saved to disk, so every
    restart begins again from `data/seed.json`. It hands out copies of its data so that a caller who
    changes a returned dict cannot accidentally change the stored data.
    """

    def __init__(self, orders: list[dict[str, Any]] | None = None) -> None:
        """Start with the given orders (tests pass their own) or, by default, the seed file's orders."""
        orders = load_seed_orders() if orders is None else orders
        self.orders: dict[str, dict[str, Any]] = {o["order_id"]: copy.deepcopy(o) for o in orders}
        self.returns: dict[str, dict[str, Any]] = {}
        self.tickets: dict[str, dict[str, Any]] = {}

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        """One order by its id, or None if there is no such order."""
        order = self.orders.get(order_id)
        return copy.deepcopy(order) if order else None

    def list_customer_orders(self, customer_id: str) -> list[dict[str, Any]]:
        """All orders that belong to one customer (in no particular order)."""
        return [copy.deepcopy(o) for o in self.orders.values() if o["customer_id"] == customer_id]

    def mark_item_returned(self, order_id: str, sku: str) -> None:
        """Flag one order line as returned, so it cannot be returned a second time."""
        item = find_item(self.orders[order_id], sku)
        if item is not None:
            item["returned"] = True

    def put_return(self, record: dict[str, Any]) -> None:
        """Save a new return record under its return_id."""
        self.returns[record["return_id"]] = copy.deepcopy(record)

    def put_ticket(self, ticket: dict[str, Any]) -> None:
        """Save a support ticket under its ticket_id."""
        self.tickets[ticket["ticket_id"]] = copy.deepcopy(ticket)

    def get_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        """One ticket by its id, or None if there is no such ticket."""
        ticket = self.tickets.get(ticket_id)
        return copy.deepcopy(ticket) if ticket else None


class DynamoStore:
    """DynamoDB-backed store (tables per design.md, GSI `customer_id-index` on orders).

    Uses three DynamoDB tables: orders (key order_id), returns (key return_id) and tickets
    (key ticket_id). The table names come from environment variables that Terraform or the boto3
    deploy script set on the Lambda and the Runtime.
    """

    def __init__(self, region: str | None = None) -> None:
        """Connect to the three DynamoDB tables in the given (or configured) AWS region."""
        import boto3  # imported lazily so memory mode has no boto3 dependency

        # boto3 is the AWS software kit for Python. A "resource" gives a simple table-level interface.
        ddb = boto3.resource("dynamodb", region_name=region or os.environ.get("AWS_REGION", "us-east-1"))
        self.orders = ddb.Table(os.environ.get("ORDERS_TABLE", "shopassist-orders"))
        self.returns = ddb.Table(os.environ.get("RETURNS_TABLE", "shopassist-returns"))
        self.tickets = ddb.Table(os.environ.get("TICKETS_TABLE", "shopassist-tickets"))

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        """Read one order by its key (DynamoDB GetItem), or None if it does not exist."""
        item = self.orders.get_item(Key={"order_id": order_id}).get("Item")
        return to_plain(item) if item else None

    def list_customer_orders(self, customer_id: str) -> list[dict[str, Any]]:
        """All orders of one customer, found through the `customer_id-index` GSI."""
        from boto3.dynamodb.conditions import Key

        # A DynamoDB Query on the index: "give me every order whose customer_id equals this one".
        kwargs: dict[str, Any] = {"IndexName": "customer_id-index",
                                  "KeyConditionExpression": Key("customer_id").eq(customer_id)}
        items: list[dict[str, Any]] = []
        # DynamoDB returns results in pages (up to 1 MB each). Keep asking for the next page,
        # starting where the last one stopped (LastEvaluatedKey), until there are no more pages.
        while True:
            page = self.orders.query(**kwargs)
            items.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                return [to_plain(i) for i in items]
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def mark_item_returned(self, order_id: str, sku: str) -> None:
        """Set `returned = true` on one item of an order stored in DynamoDB."""
        # Step 1: read the order to find the position of the item in its list of items.
        order = self.get_order(order_id)
        if order is None:
            return
        wanted = sku.strip().upper()
        for idx, item in enumerate(order["items"]):
            if str(item["sku"]).upper() == wanted:
                # Step 2: update only that one field in place (DynamoDB UpdateItem), e.g.
                # "SET items[0].returned = true". The "#" names stand in for the field names.
                self.orders.update_item(
                    Key={"order_id": order_id},
                    UpdateExpression=f"SET #items[{idx}].#returned = :t",
                    ExpressionAttributeNames={"#items": "items", "#returned": "returned"},
                    ExpressionAttributeValues={":t": True},
                )
                return

    def put_return(self, record: dict[str, Any]) -> None:
        """Save a new return record in the returns table (DynamoDB PutItem)."""
        self.returns.put_item(Item=to_dynamo(record))

    def put_ticket(self, ticket: dict[str, Any]) -> None:
        """Save a support ticket in the tickets table (DynamoDB PutItem)."""
        self.tickets.put_item(Item=to_dynamo(ticket))

    def get_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        """Read one ticket by its key, or None if it does not exist."""
        item = self.tickets.get_item(Key={"ticket_id": ticket_id}).get("Item")
        return to_plain(item) if item else None


@lru_cache(maxsize=1)
def get_store() -> Store:
    """Process-wide store chosen by `STORE` (`memory` or `dynamodb`, the default).

    Cached for warm Lambda/Runtime invocations; tests call `get_store.cache_clear()`.

    "Warm" means AWS reuses the same running copy of the Lambda or Runtime for several requests.
    The cache means we connect to DynamoDB (or load the seed file) only once per copy, not on every
    tool call. It also means a MemoryStore keeps its returns and tickets between tool calls.
    """
    if os.environ.get("STORE", "dynamodb").lower() == "memory":
        return MemoryStore()
    return DynamoStore()
