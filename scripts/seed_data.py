"""Seed the ShopAssist orders table from data/seed.json (idempotent: plain puts, same keys every run).

Relative dates (`*_days_ago`) are resolved to ISO dates at seed time, so re-run it before a demo
to refresh the return windows. Returns and tickets tables are left untouched.

Usage: uv run python scripts/seed_data.py --region us-east-1 [--prefix shopassist]

What this file does
    Loads the demo orders (customers C001 to C003, orders 1001 to 1009 and the demo order 1042) into
    the real DynamoDB orders table in AWS.

Where it fits
    Run it once after deploying, and again before a demo. Why again: the seed file says "delivered 5
    days ago", not a fixed date, so re-seeding moves every delivery date up to today and resets items
    that were returned during an earlier demo. `scripts/demo_access.py` calls `seed` itself before
    its return scenarios. Locally (memory mode) this script is not needed: the store reads
    `data/seed.json` directly.

Step by step
    1. Read data/seed.json and turn each "N days ago" into a real date (`load_seed_orders`).
    2. Convert the numbers into the Decimal type that DynamoDB requires (`to_dynamo`).
    3. Write every order to the table `<prefix>-orders`, replacing any order with the same order_id.

Key terms
    Seed data: the made-up starting data for a demo.
    Idempotent: running it twice gives the same result as running it once (no duplicate orders).
    Region: the AWS data-centre area the stack is deployed in, e.g. us-east-1.
    Prefix: the name start shared by all deployed resources (default "shopassist").
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

# Make the shared `common` package (in mcp_servers/) importable from this script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp_servers"))

from common.store import load_seed_orders, to_dynamo  # noqa: E402


def seed(region: str, prefix: str = "shopassist", dynamodb: Any = None) -> int:
    """Write every seed order to `<prefix>-orders`; returns the number of orders written.

    Tests pass a fake `dynamodb` (from the moto library); normally a real boto3 connection is made.
    """
    if dynamodb is None:
        import boto3

        dynamodb = boto3.resource("dynamodb", region_name=region)
    # Step 1: read the seed file and turn relative dates into real dates.
    orders = load_seed_orders()
    # Step 2 and 3: write all orders in batches (fewer requests to AWS). A "put" replaces an order
    # with the same order_id, which is what makes re-running safe.
    with dynamodb.Table(f"{prefix}-orders").batch_writer(overwrite_by_pkeys=["order_id"]) as batch:
        for order in orders:
            batch.put_item(Item=to_dynamo(order))
    return len(orders)


def main() -> None:
    """Read the command-line options, seed the table and print how many orders were written."""
    parser = argparse.ArgumentParser(description="Seed ShopAssist DynamoDB tables from data/seed.json")
    parser.add_argument("--region", required=True)
    parser.add_argument("--prefix", default="shopassist")
    args = parser.parse_args()
    count = seed(args.region, args.prefix)
    print(f"Seeded {count} orders into {args.prefix}-orders ({args.region})")


if __name__ == "__main__":
    main()
