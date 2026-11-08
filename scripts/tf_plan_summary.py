"""Print a Terraform plan as "address: action" lines and counts, never any attribute values.

    terraform show -json tfplan | uv run python scripts/tf_plan_summary.py            # Markdown summary
    terraform show -json tfplan | uv run python scripts/tf_plan_summary.py --hash     # one stable hash

What this file does
    Reads the JSON form of a saved plan on standard input and prints which resources would be created,
    updated, replaced or deleted, plus totals. Values (ARNs, ids, environment variables, generated
    passwords) are never printed: the repository is public, so its workflow logs are public too.
    With --hash it prints a SHA-256 of the same sorted list, so two plans can be compared.

Where it fits
    .github/workflows/ci.yml posts the summary as a pull-request comment. deploy.yml and destroy.yml
    show it in their plan job (what you approve), then re-plan after approval and refuse to apply if
    the hash differs, so what gets applied is exactly what was reviewed.

Step by step
    1. Load the plan JSON and take its resource_changes list.
    2. Turn each change's actions into one word (create, update, replace, delete, read, no-op); a
       replaced terraform_data helper becomes "rerun".
    3. Drop no-op and read entries; sort the rest by address.
    4. Print Markdown (or the hash).

Key terms
    Address: Terraform's name for a resource, e.g. module.data.aws_dynamodb_table.orders.
    Replace: delete + create (Terraform shows it as ["delete", "create"] or ["create", "delete"]).
    Re-run: a replaced terraform_data helper (for example the Gateway sync); nothing in AWS is replaced.
    SHA-256: a standard fingerprint function; the same list always gives the same 64-character hash,
      and any difference (one address or action) gives a completely different one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Any

# The marker printed in front of each address, like Terraform's own plan output (↻ = helper re-run).
SYMBOL = {"create": "+", "update": "~", "replace": "-/+", "rerun": "↻", "delete": "-"}

# terraform_data resources are helper steps (gateway sync, seeding, log groups), not AWS resources.
# Their "replace" just means "run the step again", so it isn't a reason to warn.
HELPER_TYPES = ("terraform_data",)


def action_of(actions: list[str]) -> str:
    """Step 2: Terraform's action list -> one word.

    A replacement comes as two actions (delete + create, in either order). Everything else is a
    single action, used as is; an empty list counts as no-op.
    """
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
        if act == "replace" and rc.get("type") in HELPER_TYPES:
            act = "rerun"
        if act not in ("no-op", "read"):
            out.append((rc["address"], act))
    return sorted(out)


def summary_hash(items: list[tuple[str, str]]) -> str:
    """A stable fingerprint of the change list (used to compare the approved plan with the applied one).

    Only addresses and actions go in, never values, so the hash can be printed in public logs.
    """
    return hashlib.sha256("\n".join(f"{a} {b}" for a, b in items).encode()).hexdigest()


def markdown(items: list[tuple[str, str]], title: str) -> str:
    """Step 4: the human-readable summary.

    A title, the totals, a warning when anything is replaced or destroyed, one line per change, and
    the first 16 characters of the hash (so a reviewer can match it with the deploy log).
    """
    counts = {k: sum(1 for _, a in items if a == k) for k in ("create", "update", "replace", "rerun", "delete")}
    lines = [f"### {title}", "",
             f"**{counts['create']} to add, {counts['update']} to change, {counts['replace']} to replace, "
             f"{counts['delete']} to destroy**, {counts['rerun']} helper step(s) re-run "
             "(addresses only; values are never shown in this public log)", ""]
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
    """Read the plan JSON from standard input and print the summary (or, with --hash, only the hash)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hash", action="store_true", help="print only the hash of the change list")
    parser.add_argument("--title", default="Terraform plan")
    args = parser.parse_args()
    items = changes(json.load(sys.stdin))
    print(summary_hash(items) if args.hash else markdown(items, args.title))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
