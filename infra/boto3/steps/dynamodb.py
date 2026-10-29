"""DynamoDB tables: orders, returns and tickets (on-demand), plus the demo seed data.

What this file does
    Deploy step 6. Creates the three database tables that hold the fictional store's data, then fills
    them with demo orders.

Where it fits
    The tools read and write these tables: the orders Lambda (step 7) uses `orders` and `returns`, and
    the support MCP server (step 8) uses `tickets`. The agent itself never touches them directly.

The MCP tools are the only place business data lives and rules are enforced. `orders` and `returns`
back the Lambda target, `tickets` backs the support MCP server. `orders` has a GSI on `customer_id`
so `list_customer_orders` queries instead of scanning.

Seeding runs `scripts/seed_data.py`, which turns the relative dates in `data/seed.json` into real
ones, so the return-window demo (order 1042 delivered 5 days ago) works whenever you deploy.

Step by step
    1. For each table: keep it if it exists, otherwise create it.
    2. Wait until all tables are ACTIVE (ready to use).
    3. Unless `--skip-seed` was given, run the seed script to (re)write the demo data.

Key terms
    DynamoDB: AWS's key-value database. Each row ("item") is found by its key, e.g. order_id.
    On-demand (PAY_PER_REQUEST): pay per read/write, nothing when idle; ideal for a PoC.
    Partition key (HASH key): the field that identifies a row, e.g. `order_id`.
    GSI (Global Secondary Index): a second lookup path, here "find all orders of one customer_id".
    Scan: reading the whole table (slow and costly); Query: reading only matching rows via a key or index.
"""

from __future__ import annotations

import subprocess
import sys

from common import REPO_ROOT, SEED_SCRIPT, Context, OK, ignore_not_found, info, is_not_found, tag_list


def table_specs(prefix: str) -> list[dict]:
    """The three tables: name, key field, and optional index (index name, index key field)."""
    return [
        {"name": f"{prefix}-orders", "pk": "order_id", "gsi": ("customer_id-index", "customer_id")},
        {"name": f"{prefix}-returns", "pk": "return_id", "gsi": None},
        {"name": f"{prefix}-tickets", "pk": "ticket_id", "gsi": None},
    ]


def build_create_table_request(name: str, pk: str, gsi: tuple[str, str] | None, tags: dict[str, str]) -> dict:
    """CreateTable request: on-demand billing, a text ("S" = string) key, and the index if there is one.

    Only key fields are declared up front; DynamoDB accepts any other fields on each row.
    Projection ALL copies every field into the index, so a customer lookup returns whole orders.
    """
    attrs = [{"AttributeName": pk, "AttributeType": "S"}]
    req: dict = {
        "TableName": name,
        "BillingMode": "PAY_PER_REQUEST",
        "KeySchema": [{"AttributeName": pk, "KeyType": "HASH"}],
        "AttributeDefinitions": attrs,
        "Tags": tag_list(tags),
    }
    if gsi:
        index_name, gsi_pk = gsi
        attrs.append({"AttributeName": gsi_pk, "AttributeType": "S"})
        req["GlobalSecondaryIndexes"] = [{
            "IndexName": index_name,
            "KeySchema": [{"AttributeName": gsi_pk, "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }]
    return req


def deploy(ctx: Context, seed: bool = True) -> None:
    """Create any missing tables, wait for them, then load the demo data."""
    ddb = ctx.client("dynamodb")
    tables = {}
    # Step 1: AWS API DescribeTable tells us if a table exists; "not found" means create it (CreateTable).
    # Existing tables are left as they are (their settings are not compared or updated).
    for spec in table_specs(ctx.prefix):
        name = spec["name"]
        try:
            arn = ddb.describe_table(TableName=name)["Table"]["TableArn"]
            info(f"table {name} exists")
        except Exception as err:  # noqa: BLE001
            if not is_not_found(err):
                raise
            arn = ddb.create_table(**build_create_table_request(name, spec["pk"], spec["gsi"], ctx.names.tags))[
                "TableDescription"]["TableArn"]
            info(f"{OK} creating table {name}")
        tables[name] = arn
    # Step 2: a new table takes a few seconds to become ACTIVE. The built-in "table_exists" waiter checks
    # every 3 seconds, up to 60 times (3 minutes), per table.
    waiter = ddb.get_waiter("table_exists")
    for name in tables:
        waiter.wait(TableName=name, WaiterConfig={"Delay": 3, "MaxAttempts": 60})
    info(f"{OK} all tables ACTIVE")
    ctx.state.put("dynamodb", tables=tables)

    # Step 3: seed the demo data by running scripts/seed_data.py as a separate program, with the same
    # region and prefix, so both deploy paths share one seeding script.
    if not seed:
        info("seed skipped (--skip-seed)")
        return
    if not SEED_SCRIPT.exists():
        raise RuntimeError(f"{SEED_SCRIPT} not found. Add it or re-run with --skip-seed.")
    cmd = [sys.executable, str(SEED_SCRIPT), "--region", ctx.region, "--prefix", ctx.prefix]
    info(f"seeding: {' '.join(cmd[1:])}")
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)


def destroy(ctx: Context) -> None:
    """Delete the three tables (and all their data), then wait until they are gone."""
    ddb = ctx.client("dynamodb")
    names = [spec["name"] for spec in table_specs(ctx.prefix)]
    # Step 1: request deletion of every table first (AWS API DeleteTable), so they are deleted in parallel.
    for name in names:
        if ignore_not_found(ddb.delete_table, TableName=name) is None:
            info(f"table {name} already gone")
        else:
            info(f"deleting table {name}")
    # Step 2: wait until each table has really disappeared.
    waiter = ddb.get_waiter("table_not_exists")
    for name in names:
        waiter.wait(TableName=name, WaiterConfig={"Delay": 3, "MaxAttempts": 60})
    info(f"{OK} tables deleted")
    ctx.state.drop("dynamodb")
