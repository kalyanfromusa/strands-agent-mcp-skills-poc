"""AWS Budgets `<prefix>-monthly` (optional, only with `--budget-email`): a cost guard-rail for the PoC.

What this file does
    Deploy step 12 (only when `--budget-email` is given). Creates a monthly spending limit that emails
    you when costs get close to it. It does not stop any service; it only warns.

Where it fits
    Independent of the other resources. `deploy.py` adds it near the end; `destroy.py` removes it.

A monthly COST budget of `BUDGET_LIMIT_USD` with two email alerts: actual spend above 80% and forecasted
spend above 100%. It is account-wide rather than filtered on the `project` tag, because a cost allocation
tag only works in a budget filter after it has been activated by hand in the Billing console (same choice
as the Terraform path). Budgets is a global service; the API lives in us-east-1.

The budget is replaced on every deploy (delete + create), which is the simplest way to converge its limit
and notifications; it has no history worth keeping.

Step by step
    1. Delete the existing budget, if any.
    2. Create it again with the current limit and email address.
    3. Record its name in state.json.

Key terms
    ACTUAL alert: sent when money already spent this month passes the threshold.
    FORECASTED alert: sent when AWS predicts the month will end above the threshold.
"""

from __future__ import annotations

from common import Context, OK, ignore_not_found, info, tag_list

# Monthly limit in US dollars, and the fixed region where the Budgets API lives.
BUDGET_LIMIT_USD = 5
BUDGETS_REGION = "us-east-1"


def build_create_budget_request(account_id: str, name: str, email: str, limit_usd: float,
                                tags: dict[str, str]) -> dict:
    """The CreateBudget request: a monthly cost budget with an 80% actual and a 100% forecast email alert."""
    def alert(notification_type: str, threshold: float) -> dict:
        """One email alert: "tell `email` when spend goes above `threshold` percent of the limit"."""
        return {
            "Notification": {
                "NotificationType": notification_type,
                "ComparisonOperator": "GREATER_THAN",
                "Threshold": threshold,
                "ThresholdType": "PERCENTAGE",
            },
            "Subscribers": [{"SubscriptionType": "EMAIL", "Address": email}],
        }

    return {
        "AccountId": account_id,
        "Budget": {
            "BudgetName": name,
            "BudgetType": "COST",
            "TimeUnit": "MONTHLY",
            "BudgetLimit": {"Amount": str(limit_usd), "Unit": "USD"},
        },
        "NotificationsWithSubscribers": [alert("ACTUAL", 80), alert("FORECASTED", 100)],
        "ResourceTags": tag_list(tags),
    }


def deploy(ctx: Context, email: str, limit_usd: float = BUDGET_LIMIT_USD) -> None:
    """Replace the budget (delete, then create) so its limit and alerts always match this run."""
    budgets = ctx.client("budgets", region=BUDGETS_REGION)
    name = ctx.names.budget
    # Step 1: AWS API call DeleteBudget. "Not found" just means this is the first deploy.
    if ignore_not_found(budgets.delete_budget, AccountId=ctx.account_id, BudgetName=name) is not None:
        info(f"replacing existing budget {name}")
    # Step 2: AWS API call CreateBudget with the alerts built above.
    budgets.create_budget(**build_create_budget_request(ctx.account_id, name, email, limit_usd, ctx.names.tags))
    info(f"{OK} budget {name}: {limit_usd} USD/month, alerts to {email}")
    # Step 3: remember it.
    ctx.state.put("budget", name=name, email=email)


def destroy(ctx: Context) -> None:
    """Delete the budget (fine if there never was one)."""
    budgets = ctx.client("budgets", region=BUDGETS_REGION)
    name = ctx.names.budget
    if ignore_not_found(budgets.delete_budget, AccountId=ctx.account_id, BudgetName=name) is None:
        info(f"budget {name} not present")
    else:
        info(f"{OK} deleted budget {name}")
    ctx.state.drop("budget")
