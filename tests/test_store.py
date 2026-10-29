"""Eligibility rules, seed resolution, MemoryStore vs DynamoStore parity, seed script."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from common.rules import check_eligibility, return_window_days, today_utc
from common.store import DynamoStore, MemoryStore, get_store, load_seed_orders, to_plain

REGION = "us-east-1"


def create_tables(ddb) -> None:
    """Create the three contract tables in moto."""
    ddb.create_table(
        TableName="shopassist-orders", BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "order_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "order_id", "AttributeType": "S"},
                              {"AttributeName": "customer_id", "AttributeType": "S"}],
        GlobalSecondaryIndexes=[{"IndexName": "customer_id-index",
                                 "KeySchema": [{"AttributeName": "customer_id", "KeyType": "HASH"}],
                                 "Projection": {"ProjectionType": "ALL"}}],
    )
    for name, key in (("shopassist-returns", "return_id"), ("shopassist-tickets", "ticket_id")):
        ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                         AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}])


@pytest.fixture
def aws_env(monkeypatch):
    for var, value in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                       "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION, "AWS_REGION": REGION}.items():
        monkeypatch.setenv(var, value)
    for var in ("ORDERS_TABLE", "RETURNS_TABLE", "TICKETS_TABLE", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def seeded_dynamo(aws_env):
    from scripts.seed_data import seed

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        create_tables(ddb)
        seed(REGION, dynamodb=ddb)
        yield ddb


@pytest.fixture(scope="module")
def orders() -> dict[str, dict]:
    return {o["order_id"]: o for o in load_seed_orders()}


# --- seed resolution ------------------------------------------------------------------------

def test_seed_has_expected_shape(orders):
    assert len(orders) >= 10
    assert {o["customer_id"] for o in orders.values()} == {"C001", "C002", "C003"}
    anchor = orders["1042"]
    assert anchor["customer_id"] == "C001"
    assert anchor["delivered_date"] == (today_utc() - timedelta(days=5)).isoformat()
    assert anchor["items"][0]["category"] == "electronics"
    for order in orders.values():
        assert set(order) >= {"order_id", "customer_id", "status", "order_date", "delivered_date",
                              "carrier", "tracking_number", "items", "total"}
        assert not any(k.startswith("_") or k.endswith("_days_ago") for k in order)
        assert all(isinstance(i["returned"], bool) for i in order["items"])
    assert orders["1006"]["delivered_date"] is None
    assert orders["1001"]["total"] == pytest.approx(89.90 + 2 * 14.50)


# --- eligibility rules (every seed case) ----------------------------------------------------

@pytest.mark.parametrize(("order_id", "sku", "eligible", "days", "window", "refund", "reason_part"), [
    ("1042", "EL-HP-100", True, 5, 15, 199.99, "within the 15-day"),     # demo anchor
    ("1001", "CL-JKT-001", True, 12, 30, 89.90, "within the 30-day"),    # in-window clothing
    ("1001", "cl-sck-004", True, 12, 30, 29.00, "within"),               # qty x price, sku case-insensitive
    ("1002", "HM-LMP-010", False, 45, 30, 0.0, "outside the 30-day"),    # out of window
    ("1003", "EL-SPK-020", True, 10, 15, 59.99, "within the 15-day"),    # electronics at 10 days
    ("1004", "EL-WCH-030", False, 20, 15, 0.0, "outside the 15-day"),    # electronics at 20 days
    ("1004", "CL-TSH-007", True, 20, 30, 57.00, "within the 30-day"),    # clothing at 20 days
    ("1005", "FS-SWM-040", False, 7, 30, 0.0, "final-sale"),             # final sale
    ("1005", "AC-SNG-041", True, 7, 30, 39.00, "within"),
    ("1006", "HM-MUG-050", False, None, 30, 0.0, "SHIPPED"),             # shipped, not delivered
    ("1008", "EL-CBL-070", False, None, 15, 0.0, "PROCESSING"),
    ("1009", "HM-CSH-080", False, None, 30, 0.0, "CANCELLED"),
    ("1007", "CL-HAT-060", False, 8, 30, 0.0, "already been returned"),  # already returned
    ("1007", "CL-GLV-061", True, 8, 30, 18.00, "within"),
    ("1042", "NOPE-1", False, None, 30, 0.0, "not part of order"),       # unknown sku
])
def test_eligibility_seed_cases(orders, order_id, sku, eligible, days, window, refund, reason_part):
    result = check_eligibility(orders[order_id], sku)
    assert result["eligible"] is eligible
    assert result["days_since_delivery"] == days
    assert result["return_window_days"] == window
    assert result["refund_amount"] == pytest.approx(refund)
    assert reason_part in result["reason"]


def test_eligibility_unknown_order():
    result = check_eligibility(None, "EL-HP-100")
    assert result == {"eligible": False, "reason": result["reason"], "days_since_delivery": None,
                      "return_window_days": 30, "refund_amount": 0.0}
    assert "not found" in result["reason"]


@pytest.mark.parametrize(("category", "days", "eligible"), [
    ("electronics", 15, True), ("electronics", 16, False), ("clothing", 30, True), ("clothing", 31, False),
])
def test_window_boundaries_are_inclusive(category, days, eligible):
    today = date(2026, 1, 31)
    order = {"order_id": "X", "status": "DELIVERED", "delivered_date": (today - timedelta(days=days)).isoformat(),
             "items": [{"sku": "S", "category": category, "qty": 1, "price": 10, "returned": False}]}
    assert check_eligibility(order, "S", today=today)["eligible"] is eligible
    assert return_window_days(category) == (15 if category == "electronics" else 30)


# --- stores -------------------------------------------------------------------------------

def test_to_plain_converts_decimals():
    assert to_plain({"a": Decimal("2"), "b": [Decimal("1.5")], "c": "x"}) == {"a": 2, "b": [1.5], "c": "x"}
    assert type(to_plain(Decimal("2"))) is int


def test_get_store_selects_by_env(monkeypatch, aws_env):
    monkeypatch.setenv("STORE", "memory")
    get_store.cache_clear()
    try:
        assert isinstance(get_store(), MemoryStore)
        assert get_store() is get_store()
        monkeypatch.setenv("STORE", "dynamodb")
        get_store.cache_clear()
        assert isinstance(get_store(), DynamoStore)
    finally:
        get_store.cache_clear()


def test_memory_store_is_isolated_from_callers():
    store = MemoryStore()
    order = store.get_order("1042")
    order["items"][0]["returned"] = True
    assert store.get_order("1042")["items"][0]["returned"] is False
    store.mark_item_returned("1042", "el-hp-100")
    assert store.get_order("1042")["items"][0]["returned"] is True


def test_memory_and_dynamo_parity(seeded_dynamo):
    memory, dynamo = MemoryStore(), DynamoStore(region=REGION)
    for order_id in ("1001", "1004", "1006", "1042"):
        assert dynamo.get_order(order_id) == memory.get_order(order_id)
    assert dynamo.get_order("9999") is None and memory.get_order("9999") is None

    def ids(store, customer):
        return sorted(o["order_id"] for o in store.list_customer_orders(customer))

    for customer in ("C001", "C002", "C003", "C999"):
        assert ids(dynamo, customer) == ids(memory, customer)

    for store in (memory, dynamo):
        store.mark_item_returned("1001", "CL-JKT-001")
        store.put_ticket({"ticket_id": "TCK-000001", "customer_id": "C001", "status": "OPEN", "score": 1.5})
    assert dynamo.get_order("1001") == memory.get_order("1001")
    assert dynamo.get_order("1001")["items"][0]["returned"] is True
    assert dynamo.get_ticket("TCK-000001") == memory.get_ticket("TCK-000001")
    assert dynamo.get_ticket("TCK-NOPE") is None

    order = dynamo.get_order("1042")
    assert isinstance(order["total"], float) and isinstance(order["items"][0]["qty"], int)


def test_seed_script_is_idempotent(seeded_dynamo):
    from scripts.seed_data import seed

    table = seeded_dynamo.Table("shopassist-orders")
    first = table.scan()["Items"]
    assert seed(REGION, dynamodb=seeded_dynamo) == len(first)
    assert len(table.scan()["Items"]) == len(first)
