"""Print a Terraform plan as "address: action" lines and counts, never any attribute values.

    terraform show -json tfplan | uv run python scripts/tf_plan_summary.py            # Markdown summary
    terraform show -json tfplan | uv run python scripts/tf_plan_summary.py --hash     # one stable hash

What this file does
    Reads the JSON form of a saved plan on standard input and prints which resources would be created,
    updated, replaced or deleted, plus totals. Values (ARNs, ids, environment variables, generated
    passwords) are never printed: the repository is public, so its workflow logs are public too.
    With --hash it prints a SHA-256 of the same sorted list, so two plans can be compared.

Where it fits
    .github/workflows/ci.yml posts the summary as a pull-request comment. deploy.yml shows it in the
    plan job (what you approve), then re-plans after approval and refuses to apply if the hash differs,
    so what gets applied is exactly what was reviewed.

Step by step
    1. Load the plan JSON and take its resource_changes list.
    2. Turn each change's actions into one word (create, update, replace, delete, read, no-op).
    3. Drop no-op and read entries; sort the rest by address.
    4. Print Markdown (or the hash).

Key terms
    Address: Terraform's name for a resource, e.g. module.data.aws_dynamodb_table.orders.
    Replace: delete + create (Terraform shows it as ["delete", "create"] or ["create", "delete"]).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Any

SYMBOL = {"create": "+", "update": "~", "replace": "-/+", "delete": "-"}


def action_of(actions: list[str]) -> str:
    """Step 2: Terraform's action list -> one word."""
    if sorted(actions) == ["create", "delete"]:
        return "replace"
    if actions in (["no-op"], ["read"]):
        return actions[0]
    return actions[0] if actions else "no-op"


def changes(plan: dict[str, Any]) -> list[tuple[str, str]]:
    """Steps 1 and 3: sorted (address, action) pairs that really change something."""
    out = []
    for rc in plan.get("resource_changes", []):
        act = action_of(rc.get("change", {}).get("actions", []))
        if act not in ("no-op", "read"):
            out.append((rc["address"], act))
    return sorted(out)


def summary_hash(items: list[tuple[str, str]]) -> str:
    """A stable fingerprint of the change list (used to compare the approved plan with the applied one)."""
    return hashlib.sha256("\n".join(f"{a} {b}" for a, b in items).encode()).hexdigest()


def markdown(items: list[tuple[str, str]], title: str) -> str:
    """Step 4: the human-readable summary."""
    counts = {k: sum(1 for _, a in items if a == k) for k in ("create", "update", "replace", "delete")}
    lines = [f"### {title}", "",
             f"**{counts['create']} to add, {counts['update']} to change, {counts['replace']} to replace, "
             f"{counts['delete']} to destroy** (addresses only; values are never shown in this public log)", ""]
    if counts["replace"] or counts["delete"]:
        lines += ["> ⚠️ This plan replaces or destroys resources. Check the list before approving.", ""]
    if items:
        lines.append("```")
        lines += [f"{SYMBOL.get(a, '?'):>3} {addr}  ({a})" for addr, a in items]
        lines.append("```")
    else:
        lines.append("No changes.")
    lines += ["", f"<sub>plan hash `{summary_hash(items)[:16]}`</sub>"]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hash", action="store_true", help="print only the hash of the change list")
    parser.add_argument("--title", default="Terraform plan")
    args = parser.parse_args()
    items = changes(json.load(sys.stdin))
    print(summary_hash(items) if args.hash else markdown(items, args.title))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
