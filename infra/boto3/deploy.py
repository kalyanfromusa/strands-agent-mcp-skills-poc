"""Deploy ShopAssist with plain boto3 calls, in dependency order (the imperative twin of infra/terraform).

    uv run python scripts/build.py                     # builds dist/*.zip first
    uv run python infra/boto3/deploy.py [--region us-east-1] [--prefix shopassist]
                                        [--model-id us.anthropic.claude-haiku-5-5]
                                        [--skip-seed] [--budget-email you@example.com]

What this file does
    Creates (or updates) every AWS resource the ShopAssist PoC needs, one step at a time, by calling the
    AWS APIs directly through boto3. "Imperative" means we spell out each action in order ("create the
    bucket, then upload the files..."), unlike Terraform, where you describe the end result and the tool
    works out the actions.

Where it fits
    You run it by hand after `scripts/build.py` has built the code zips. It calls the `deploy` function of
    each module in `steps/` in the order below. When it finishes, it writes `.deploy/outputs.json`, which
    `scripts/invoke_agent.py` and `scripts/demo_access.py` read to find the deployed agent.

Every step is get-or-create-or-update, so the script can be re-run at any time: after a crash it resumes,
after a code change it rolls the change out. IDs/ARNs are saved to `infra/boto3/state.json` after every
step; `destroy.py` uses them (and falls back to name lookups). The result is `.deploy/outputs.json`, the
same contract file Terraform writes. Deploy only one of the two paths at a time: they use the same names.

Step by step (the order matters: each step needs something an earlier step created)
  1  S3 bucket, code zips, skills  everything below reads artifacts from S3
  2  Cognito pool, clients, users  the OAuth2 server for every JWT
  3  AgentCore Identity provider   holds the M2M client secret; the Gateway uses it outbound
  4  Policy engine                 the Gateway references it, and the Gateway role is scoped to it
  5  IAM roles                     need the provider (secret ARN) and the engine ARN
  6  DynamoDB tables (+ seed)
  7  Lambda (orders tools)
  8  Support MCP runtime           must be READY before the Gateway lists its tools
  9  Gateway + targets             orders (Lambda) then support (MCP server)
 10  Cedar policies                validated against the Gateway's tools, so after the targets
 11  Agent runtime                 needs the Gateway URL
 12  Budget (only with --budget-email)
 13  .deploy/outputs.json

In business terms
    S3 bucket = file storage for the code packages and the skill instructions.
    Cognito = the user directory and login service (users alice, bob, carol, plus a machine login).
    AgentCore Identity = a safe that keeps the machine login secret, so no code has to hold it.
    Policy engine (Cedar) = the rulebook that says who may call which tool.
    IAM roles = the permission sets each component runs with.
    DynamoDB = the database tables (orders, returns, tickets).
    Lambda = a small on-demand function that runs the orders tools.
    AgentCore Runtime = managed hosting for code; used twice, for the support tools and for the agent.
    Gateway = the single front door the agent uses to reach all tools.

Key terms
    MCP (Model Context Protocol): a standard way for an AI agent to discover and call tools.
    JWT (JSON Web Token): a signed login token that says who the user is and which groups they are in.
    OAuth2: the standard rules for getting and checking such tokens.
    M2M (machine to machine): a program logging in with an ID and secret, with no person involved.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Make `common` and `steps` importable when the script is run from any folder.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (ARTIFACTS, DEFAULT_MODEL_ID, DEFAULT_PREFIX, DEFAULT_REGION, PREFIX_PATTERN, REPO_ROOT,  # noqa: E402
                    STATE_PATH, OK, Progress, State, fmt_seconds, make_context)
from steps import (agent_runtime, budget, cognito, dynamodb, gateway, iam, identity, lambda_tools,  # noqa: E402
                   mcp_runtime, outputs, policy, s3)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Read the command-line options and check that the prefix is a valid name for every AWS service."""
    p = argparse.ArgumentParser(description="Deploy ShopAssist with boto3 (imperative SDK path).")
    p.add_argument("--region", default=DEFAULT_REGION, help=f"AWS region (default {DEFAULT_REGION})")
    p.add_argument("--prefix", default=DEFAULT_PREFIX, help=f"name prefix and project tag (default {DEFAULT_PREFIX})")
    p.add_argument("--model-id", default=DEFAULT_MODEL_ID, help=f"Bedrock model or inference profile (default {DEFAULT_MODEL_ID})")
    p.add_argument("--skip-seed", action="store_true", help="don't (re)write the demo orders to DynamoDB")
    p.add_argument("--budget-email", help="create a monthly AWS Budget with alerts to this address")
    args = p.parse_args(argv)
    if not PREFIX_PATTERN.match(args.prefix):
        p.error(f"--prefix must match {PREFIX_PATTERN.pattern} (lowercase letters and digits, 2-21 chars)")
    return args


def check_artifacts() -> None:
    """Stop early, before touching AWS, if the code zips have not been built yet."""
    missing = [path for path in ARTIFACTS.values() if not path.is_file()]
    if missing:
        names = ", ".join(path.relative_to(REPO_ROOT).as_posix() for path in missing)
        sys.exit(f"Missing build artifacts: {names}\nRun `uv run python scripts/build.py` first.")


def check_state(args: argparse.Namespace) -> None:
    """Refuse to mix two stacks in one state file (destroy would then miss the first one)."""
    old = State().data
    if old.get("resources") and (old.get("prefix"), old.get("region")) != (args.prefix, args.region):
        sys.exit(f"{STATE_PATH.name} tracks prefix {old.get('prefix')!r} in {old.get('region')}. Destroy that "
                 f"stack first (destroy.py --prefix {old.get('prefix')} --region {old.get('region')}) or move the file.")


def steps(args: argparse.Namespace):
    """(title, function(ctx)) in deploy order.

    This list IS the deployment plan. See the module docstring for why each step comes where it does.
    The budget step is only added when an email address was given.
    """
    plan = [
        ("S3 artifacts bucket, code zips and skills", s3.deploy),
        ("Cognito user pool, domain, app clients, groups and demo users", cognito.deploy),
        ("AgentCore Identity OAuth2 credential provider", identity.deploy),
        ("AgentCore Policy engine", policy.deploy_engine),
        ("IAM execution roles", iam.deploy),
        ("DynamoDB tables" + ("" if args.skip_seed else " and seed data"),
         lambda ctx: dynamodb.deploy(ctx, seed=not args.skip_seed)),
        ("Lambda orders tools", lambda_tools.deploy),
        ("AgentCore Runtime: support MCP server", mcp_runtime.deploy),
        ("AgentCore Gateway and targets (orders, support)", gateway.deploy),
        ("Cedar policies", policy.deploy_policies),
        ("AgentCore Runtime: agent", agent_runtime.deploy),
    ]
    if args.budget_email:
        plan.append(("AWS Budget", lambda ctx: budget.deploy(ctx, args.budget_email)))
    plan.append(("Deployment outputs", outputs.deploy))
    return plan


def main(argv: list[str] | None = None) -> int:
    """Run the whole deployment from start to finish and print how to try the agent."""
    # Step 1: read options and run the safety checks (built zips present, no other stack in state.json).
    args = parse_args(argv)
    check_artifacts()
    check_state(args)
    # Step 2: connect to AWS and load what a previous run already created.
    ctx = make_context(args.region, args.prefix, args.model_id)
    previous = ctx.state.data.get("resources")
    print(f"Deploying {args.prefix} to account {ctx.account_id}, region {args.region} (model {args.model_id})")
    if previous:
        print(f"Resuming with existing state ({STATE_PATH.name}: {', '.join(previous)})")

    # Step 3: run each step in order. On any error, save the state and stop; the user fixes the cause and
    # simply runs the script again, which skips everything that is already in place.
    plan = steps(args)
    progress = Progress(len(plan))
    for title, fn in plan:
        try:
            with progress.step(title):
                fn(ctx)
        except Exception:
            ctx.state.save()
            print(f"\nDeploy stopped at step {progress.n}/{progress.total} after {progress.elapsed()}. "
                  "Fix the cause and re-run (every step is idempotent), or clean up with "
                  "`uv run python infra/boto3/destroy.py --yes`.", file=sys.stderr)
            raise
        # Record progress after every successful step.
        ctx.state.data["last_completed_step"] = title
        ctx.state.data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ctx.state.save()

    # Step 4: print a summary and the commands to try the deployed agent.
    out = ctx.state.data["resources"]
    print(f"\n{OK} Deployed in {progress.elapsed()}")
    print(f"  agent runtime : {out['agent_runtime']['arn']}")
    print(f"  gateway       : {out['gateway']['url']}")
    print("  outputs       : .deploy/outputs.json   users: .deploy/users.json")
    print("\nTry it:")
    print('  uv run python scripts/invoke_agent.py --user alice --prompt "Where is my order 1042?"')
    print("  uv run python scripts/demo_access.py")
    return 0


if __name__ == "__main__":
    t0 = time.monotonic()
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Ctrl+C: state.json is saved after every change, so the next run carries on from here.
        sys.exit(f"\nInterrupted after {fmt_seconds(time.monotonic() - t0)}; state saved, re-run to resume.")
