"""Return-eligibility rules. Pure functions, no I/O, so they are easy to test.

What this file does
    Decides whether one item of a customer's order can be returned, and how much money the customer
    would get back. It only looks at the order data it is given; it never reads or writes a database.

Where it fits
    The orders Lambda (`orders_lambda/handler.py`) calls `check_eligibility` for two tools:
    `check_return_eligibility` (just asks "can this be returned?") and `create_return` (asks again
    right before it saves a return, so the rule cannot be skipped). The store (`store.py`) reuses
    `find_item` and `today_utc`. The skill file `skills/returns-and-refunds/references/return-policy.md`
    only *describes* these rules to the AI model; this file is where they are actually *enforced*.

Step by step (what `check_eligibility` does, in order)
    1. If there is no order, answer "not eligible: order not found".
    2. Find the item in the order by its SKU (product code). If it is not there, answer "not eligible".
    3. Work out the return window for the item's category (electronics 15 days, everything else 30)
       and the refund (price x quantity).
    4. Count the days since delivery. If the order is not DELIVERED (or has no delivery date), answer
       "not eligible".
    5. If the item was already returned, answer "not eligible".
    6. If the item's category is `final-sale`, answer "not eligible" (final-sale is never returnable).
    7. If more days have passed than the window allows, answer "not eligible".
    8. Otherwise answer "eligible" and include the refund amount.
    The first rule that fails decides the answer, so the reason text always names exactly one problem.

Key terms
    SKU: "stock keeping unit", the product code of an order line, e.g. "EL-HP-100".
    Return window: the number of days after delivery during which an item may be returned.
    UTC: Coordinated Universal Time, one clock for everyone, so the answer does not depend on where the
        server runs.
    ISO date: a date written as YYYY-MM-DD, e.g. "2026-10-24".
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

# The return policy as data. Any category that is not listed in RETURN_WINDOW_DAYS gets the default
# 30 days. Only "electronics" has a shorter window (15 days). Category names are compared in lower case.
DEFAULT_RETURN_WINDOW_DAYS = 30
RETURN_WINDOW_DAYS: dict[str, int] = {"electronics": 15}
# Items in this category can never be returned, whatever the number of days.
FINAL_SALE_CATEGORY = "final-sale"


def today_utc() -> date:
    """Today's date in UTC (the reference date for every return window).

    Using UTC everywhere means a Lambda in the United States and a laptop in Europe count the same
    number of days since delivery.
    """
    return datetime.now(UTC).date()


def return_window_days(category: str | None) -> int:
    """Return window in days for an item category.

    Example: "electronics" -> 15, "clothing" -> 30, a missing category -> 30.
    """
    return RETURN_WINDOW_DAYS.get((category or "").lower(), DEFAULT_RETURN_WINDOW_DAYS)


def find_item(order: dict[str, Any], sku: str) -> dict[str, Any] | None:
    """Return the order line with this SKU (case-insensitive), or None.

    Spaces around the SKU are ignored and upper/lower case does not matter, because the AI model may
    pass "el-hp-100 " when the customer typed it that way.
    """
    wanted = sku.strip().upper()
    return next((i for i in order.get("items", []) if str(i.get("sku", "")).upper() == wanted), None)


def check_eligibility(order: dict[str, Any] | None, sku: str, today: date | None = None) -> dict[str, Any]:
    """Apply the return policy to one order line.

    Rules: the order must be DELIVERED, the item must exist and not already be returned,
    `final-sale` items are never returnable, and the window (electronics 15 days, everything
    else 30 days, inclusive) counts from `delivered_date`. Refund = price x qty.

    "Inclusive" means an item delivered exactly 15 days ago (electronics) or 30 days ago (other
    categories) can still be returned; one day more and it cannot.

    `today` can be passed in by tests to pin the date; normally it is today's UTC date.
    The answer always has the same five fields: eligible (yes/no), reason (a sentence the agent can
    repeat to the customer), days_since_delivery, return_window_days and refund_amount (0 unless
    eligible).
    """
    today = today or today_utc()

    # Helper that builds the answer in one fixed shape, so every exit below returns the same fields.
    # The refund is rounded to cents.
    def result(eligible: bool, reason: str, window: int = DEFAULT_RETURN_WINDOW_DAYS,
               days: int | None = None, refund: float = 0.0) -> dict[str, Any]:
        return {
            "eligible": eligible,
            "reason": reason,
            "days_since_delivery": days,
            "return_window_days": window,
            "refund_amount": round(refund, 2),
        }

    # Step 1: the order must exist.
    if order is None:
        return result(False, "Order not found. Check the order number with the customer.")
    order_id = order["order_id"]
    # Step 2: the item (SKU) must be part of this order.
    item = find_item(order, sku)
    if item is None:
        return result(False, f"Item {sku} is not part of order {order_id}.")

    # Step 3: look up the window for this category and work out the possible refund.
    category = str(item.get("category", ""))
    window = return_window_days(category)
    refund = float(item.get("price", 0)) * int(item.get("qty", 1))
    # Step 4: count whole days from the delivery date to today (None if never delivered).
    delivered = order.get("delivered_date")
    days = (today - date.fromisoformat(delivered)).days if delivered else None

    # Step 4 (continued): only delivered orders can be returned. Orders that are PROCESSING, SHIPPED or
    # CANCELLED stop here.
    if order.get("status") != "DELIVERED" or days is None:
        return result(False, f"Order {order_id} is {order.get('status')}, not DELIVERED. "
                             "Only delivered orders can be returned.", window)
    # Step 5: an item can only be returned once.
    if item.get("returned"):
        return result(False, f"Item {item['sku']} from order {order_id} has already been returned.",
                      window, days)
    # Step 6: final-sale items are never returnable.
    if category.lower() == FINAL_SALE_CATEGORY:
        return result(False, f"Item {item['sku']} is a final-sale item and cannot be returned.",
                      window, days)
    # Step 7: too late? (The last day of the window still counts, hence ">" and not ">=".)
    if days > window:
        return result(False, f"Delivered {days} days ago, outside the {window}-day return window "
                             f"for {category} items.", window, days)
    # Step 8: every rule passed, so the item is eligible and the refund is price x quantity.
    return result(True, f"Delivered {days} days ago, within the {window}-day return window "
                        f"for {category} items.", window, days, refund)
