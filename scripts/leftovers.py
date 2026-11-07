"""Find (and optionally remove) every trace of ShopAssist left in an AWS account after a destroy.

    uv run python scripts/leftovers.py --region us-east-1 --env prod            # report; exit 1 if anything is left
    uv run python scripts/leftovers.py --region us-east-1 --env prod --sweep    # also remove what Terraform can't
    uv run python scripts/leftovers.py --region us-east-1 --all                 # every env + platform + state bucket

What this file does
    Looks for ShopAssist resources by NAME (shopassist-<env>-*, shopassist_<env>_*) and by TAG
    (project=shopassist, environment=<env>) in every service the project uses, and prints what it finds.
    With --sweep it also removes the things that outlive `terraform destroy`:
      - container images in the ECR repositories (the repositories themselves belong to the platform);
      - AgentCore Identity secrets still waiting in Secrets Manager's recovery window (force-deleted now,
        so nothing is recoverable or billed for the next 7-30 days);
      - log groups that AgentCore created on its own (for example for runtime versions);
      - the environment's Terraform state file in the state bucket.
    It exits 1 if anything is still there at the end, so a workflow fails loudly instead of leaving cost.

Where it fits
    The last step of .github/workflows/destroy.yml (one environment) and of
    scripts/destroy_everything.sh (--all). It's also a handy read-only check at any time.

Step by step
    1. Build the name patterns for the chosen scope (one environment, or everything).
    2. With --sweep: delete ECR images, scheduled secrets, stray log groups, the env state file.
    3. Scan every service: AgentCore (runtimes, gateways, policy engines, credential providers,
       workload identities), Lambda, DynamoDB, S3, Cognito, IAM roles and policies, CloudWatch logs,
       Secrets Manager (including secrets scheduled for deletion), ECR, Budgets and the tagging API.
    4. Print the findings and exit 0 (clean) or 1 (something left).

Key terms
    Recovery window: Secrets Manager normally keeps a deleted secret for 7-30 days so it can be
      restored. A clean sweep removes it immediately.
    Tagging API: one AWS call that lists resources by tag across most services.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable, Iterable

import boto3
from botocore.exceptions import ClientError

PROJECT = "shopassist"


def patterns(env: str | None) -> list[re.Pattern[str]]:
    """Step 1: names that belong to the scope. env=None means every ShopAssist resource."""
    if env is None:
        return [re.compile(rf"^{PROJECT}[-_]", re.I), re.compile(rf"/{PROJECT}[-_]", re.I)]
    return [re.compile(rf"(^|/){PROJECT}[-_]{env}[-_]", re.I)]


# Owned by the platform stack, not by an environment: an environment destroy must leave them alone.
PLATFORM_OWNED = re.compile(rf"^{PROJECT}-(dev|prod)-boundary$|^{PROJECT}-gha-|^{PROJECT}-tfstate-")


def matches(name: str, pats: list[re.Pattern[str]], env: str | None = "x") -> bool:
    if env is not None and PLATFORM_OWNED.search(name):
        return False
    return any(p.search(name) for p in pats)


def paged(client, op: str, key: str, **kw) -> Iterable[dict]:
    """Iterate over every item of a paginated list call (with or without a boto3 paginator)."""
    if client.can_paginate(op):
        for page in client.get_paginator(op).paginate(**kw):
            yield from page.get(key, [])
        return
    token = None
    while True:
        page = getattr(client, op)(**kw, **({"nextToken": token} if token else {}))
        yield from page.get(key, [])
        token = page.get("nextToken")
        if not token:
            return


def safe(label: str, fn: Callable[[], list[str]], problems: list[str]) -> list[str]:
    """Run one check; an access error is reported as a problem instead of crashing the whole scan."""
    try:
        return fn()
    except ClientError as e:
        problems.append(f"{label}: could not check ({e.response['Error']['Code']})")
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Find (and with --sweep remove) ShopAssist leftovers.")
    parser.add_argument("--region", required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--env", choices=["dev", "prod"], help="one environment")
    scope.add_argument("--all", action="store_true", help="every environment, the platform and the state bucket")
    parser.add_argument("--sweep", action="store_true", help="remove what outlives terraform destroy")
    parser.add_argument("--keep-images", action="store_true", help="with --sweep: don't delete ECR images")
    args = parser.parse_args()

    region, env = args.region, (None if args.all else args.env)
    pats = patterns(env)
    session = boto3.session.Session(region_name=region)
    account = session.client("sts").get_caller_identity()["Account"]
    c = session.client
    found: dict[str, list[str]] = {}
    problems: list[str] = []
    print(f"Scanning account {account}, {region}, scope: {'everything' if env is None else env}")

    # ---------------------------------------------------------------- Step 2: sweep
    if args.sweep:
        ecr = c("ecr")
        if not args.keep_images:
            for repo in paged(ecr, "describe_repositories", "repositories"):
                if not repo["repositoryName"].startswith(f"{PROJECT}-"):
                    continue
                ids = list(paged(ecr, "list_images", "imageIds", repositoryName=repo["repositoryName"]))
                for i in range(0, len(ids), 100):
                    ecr.batch_delete_image(repositoryName=repo["repositoryName"], imageIds=ids[i:i + 100])
                if ids:
                    print(f"  swept {len(ids)} image(s) from {repo['repositoryName']}")
        sm = c("secretsmanager")
        for s in paged(sm, "list_secrets", "SecretList", IncludePlannedDeletion=True):
            if "bedrock-agentcore-identity" in s["Name"] and matches(s["Name"].split("/")[-1], pats, env):
                sm.delete_secret(SecretId=s["ARN"], ForceDeleteWithoutRecovery=True)
                print(f"  swept secret {s['Name']} (no recovery window)")
        logs = c("logs")
        for g in paged(logs, "describe_log_groups", "logGroups"):
            if matches(g["logGroupName"], pats, env):
                logs.delete_log_group(logGroupName=g["logGroupName"])
                print(f"  swept log group {g['logGroupName']}")
        if env is not None:
            bucket = f"{PROJECT}-tfstate-{account}-{region}"
            try:
                c("s3").delete_object(Bucket=bucket, Key=f"main/{env}.tfstate")
                print(f"  swept state file s3://{bucket}/main/{env}.tfstate")
            except ClientError as e:
                if e.response["Error"]["Code"] not in ("NoSuchBucket", "NoSuchKey"):
                    problems.append(f"state file: {e.response['Error']['Code']}")

    # ---------------------------------------------------------------- Step 3: scan
    ac = c("bedrock-agentcore-control")
    checks: dict[str, Callable[[], list[str]]] = {
        "AgentCore runtimes": lambda: [r["agentRuntimeName"] for r in paged(ac, "list_agent_runtimes", "agentRuntimes")],
        "AgentCore gateways": lambda: [g["name"] for g in paged(ac, "list_gateways", "items")],
        "AgentCore policy engines": lambda: [p["name"] for p in paged(ac, "list_policy_engines", "policyEngines")],
        "AgentCore credential providers": lambda: [p["name"] for p in paged(ac, "list_oauth2_credential_providers", "credentialProviders")],
        "AgentCore workload identities": lambda: [w["name"] for w in paged(ac, "list_workload_identities", "workloadIdentities")],
        "Lambda functions": lambda: [f["FunctionName"] for f in paged(c("lambda"), "list_functions", "Functions")],
        "DynamoDB tables": lambda: list(paged(c("dynamodb"), "list_tables", "TableNames")),
        "S3 buckets": lambda: [b["Name"] for b in c("s3").list_buckets()["Buckets"]],
        "Cognito user pools": lambda: [p["Name"] for p in paged(c("cognito-idp"), "list_user_pools", "UserPools", MaxResults=60)],
        "IAM roles": lambda: [r["RoleName"] for r in paged(c("iam"), "list_roles", "Roles")],
        "IAM policies": lambda: [p["PolicyName"] for p in paged(c("iam"), "list_policies", "Policies", Scope="Local")],
        "CloudWatch log groups": lambda: [g["logGroupName"] for g in paged(c("logs"), "describe_log_groups", "logGroups")],
        "Secrets (incl. scheduled deletion)": lambda: [s["Name"].split("/")[-1] for s in paged(c("secretsmanager"), "list_secrets", "SecretList", IncludePlannedDeletion=True) if "bedrock-agentcore-identity" in s["Name"] or s["Name"].startswith(PROJECT)],
        "Budgets": lambda: [b["BudgetName"] for b in c("budgets", region_name="us-east-1").describe_budgets(AccountId=account).get("Budgets", [])],
    }
    for label, fn in checks.items():
        hits = [n for n in safe(label, fn, problems) if matches(n, pats, env)]
        if hits:
            found[label] = hits

    # ECR: with --all the repositories themselves are leftovers; for one env only images, and only when sweeping them.
    def ecr_check() -> list[str]:
        ecr, out = c("ecr"), []
        for repo in paged(ecr, "describe_repositories", "repositories"):
            name = repo["repositoryName"]
            if not name.startswith(f"{PROJECT}-"):
                continue
            n = len(list(paged(ecr, "list_images", "imageIds", repositoryName=name)))
            if env is None:
                out.append(f"{name} (repository, {n} image(s))")
            elif n and args.sweep and not args.keep_images:
                # Images are shared by all environments: they only count as left over when this run was
                # asked to delete them (destroy with delete_images = true).
                out.append(f"{name}: {n} image(s)")
        return out
    ecr_hits = safe("ECR", ecr_check, problems)
    if ecr_hits:
        found["ECR"] = ecr_hits

    def tagged() -> list[str]:
        filters = [{"Key": "project", "Values": [PROJECT]}]
        if env is not None:
            filters.append({"Key": "environment", "Values": [env]})
        arns = [r["ResourceARN"] for r in paged(c("resourcegroupstaggingapi"), "get_resources", "ResourceTagMappingList", TagFilters=filters)]
        # The tagging index lags behind deletions; only report ARNs not already explained above.
        return [a for a in arns if not any(n.split(" ")[0] in a for hits in found.values() for n in hits)]
    tag_hits = safe("Tagging API", tagged, problems)
    if tag_hits:
        found["Tagged (project=shopassist) - may be index lag, re-check in a few minutes"] = tag_hits

    if env is None:
        try:
            c("iam").get_open_id_connect_provider(
                OpenIDConnectProviderArn=f"arn:aws:iam::{account}:oidc-provider/token.actions.githubusercontent.com")
            found["IAM OIDC provider"] = ["token.actions.githubusercontent.com (created by the platform stack)"]
        except ClientError:
            pass

    # ---------------------------------------------------------------- Step 4: report
    if not found and not problems:
        print("CLEAN: no ShopAssist resources left in this scope.")
        return 0
    for label, hits in found.items():
        print(f"LEFT  {label}:")
        for h in hits:
            print(f"        {h}")
    for p in problems:
        print(f"WARN  {p}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
