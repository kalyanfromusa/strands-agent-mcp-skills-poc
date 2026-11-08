"""Find (and optionally remove) every trace of ShopAssist left in an AWS account after a destroy.

    uv run python scripts/leftovers.py --region us-east-1 --env prod            # report; exit 1 if anything is left
    uv run python scripts/leftovers.py --region us-east-1 --env prod --sweep    # also remove what Terraform can't
    uv run python scripts/leftovers.py --region us-east-1 --all                 # every env + platform + state bucket

What this file does
    Looks for ShopAssist resources by NAME (shopassist-<env>-*, shopassist_<env>_*) and by TAG
    (project=shopassist, environment=<env>) in every service the project uses, and prints what it finds.
    With --sweep it also removes the things that outlive `terraform destroy`:
      - container images in every shopassist-* ECR (Elastic Container Registry) repository (the
        repositories themselves belong to the platform stack); skipped with --keep-images, because
        dev and prod share the repositories;
      - AgentCore Identity secrets of the scope (Secrets Manager names containing
        "bedrock-agentcore-identity"), whether still live or already waiting in the recovery window.
        They are force-deleted, so nothing is recoverable or billed for the next 7-30 days;
      - every CloudWatch log group whose name matches the scope, typically the ones AgentCore created
        on its own (for example for runtime versions);
      - the environment's Terraform state file in the state bucket (one-environment scope only).
    It exits 1 if any resource is still there at the end, so a workflow fails loudly instead of
    leaving cost. A check that couldn't run (for example access denied) is printed as WARN but does
    not by itself make it exit 1.

Where it fits
    The last step of .github/workflows/destroy.yml (--env <env> --sweep) and of
    scripts/destroy_everything.sh (--env <env> --sweep after each environment, then --all at the end).
    Without --sweep it only reads, so it's also a handy check at any time.

Step by step
    1. Build the name patterns for the chosen scope (one environment, or everything).
    2. With --sweep: delete ECR images, the scope's AgentCore Identity secrets, matching log groups,
       the env state file.
    3. Scan every service: AgentCore (runtimes, gateways, policy engines, credential providers,
       workload identities), Lambda, DynamoDB, S3, Cognito, IAM roles and policies, CloudWatch logs,
       Secrets Manager (including secrets scheduled for deletion), ECR, Budgets and the tagging API.
    4. Print the findings and exit 0 (clean) or 1 (something left).

Key terms
    Scope: which resources count. --env prod = only shopassist-prod-* / shopassist_prod_* (and never
      the platform's own roles, boundaries or state bucket); --all = everything named shopassist-*.
    Recovery window: Secrets Manager normally keeps a deleted secret for 7-30 days so it can be
      restored. A clean sweep removes it immediately.
    AgentCore Identity: the AgentCore service that stores OAuth (sign-in) client secrets for the
      Gateway; it keeps them in Secrets Manager.
    Tagging API: one AWS call that lists resources by tag across most services. Its index can lag
      behind, so a just-deleted resource may still show up there for a while (reported as NOTE).
    Pagination: AWS returns long lists page by page; `paged` below fetches every page.
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
    """Step 1: names that belong to the scope. env=None means every ShopAssist resource.

    Everything: a name that starts with shopassist- / shopassist_, or has it right after a "/"
    (log group paths). One env: shopassist-<env>- or shopassist_<env>_ at the start or after a "/".
    Case is ignored.
    """
    if env is None:
        return [re.compile(rf"^{PROJECT}[-_]", re.I), re.compile(rf"/{PROJECT}[-_]", re.I)]
    return [re.compile(rf"(^|/){PROJECT}[-_]{env}[-_]", re.I)]


# Owned by the platform stack, not by an environment: an environment destroy must leave them alone.
# (The permissions boundaries shopassist-<env>-boundary, the CI roles shopassist-gha-*, the state bucket.)
PLATFORM_OWNED = re.compile(rf"^{PROJECT}-(dev|prod)-boundary$|^{PROJECT}-gha-|^{PROJECT}-tfstate-")


def matches(name: str, pats: list[re.Pattern[str]], env: str | None = "x") -> bool:
    """Does this resource name belong to the scope?

    With one environment (any env other than None; the default "x" just means "not everything"),
    platform-owned names never count, so an environment destroy doesn't report or sweep the
    foundation. With env=None (--all) they count like everything else.
    """
    if env is not None and PLATFORM_OWNED.search(name):
        return False
    return any(p.search(name) for p in pats)


def paged(client, op: str, key: str, **kw) -> Iterable[dict]:
    """Iterate over every item of a paginated list call (with or without a boto3 paginator).

    `op` is the list operation's name, `key` the field in each page that holds the items. If boto3
    has no ready-made paginator for the operation, follow `nextToken` by hand until it runs out.
    """
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
    """Read the options, optionally sweep, scan every service, print the report. Returns the exit code."""
    # Options: --region, and exactly one of --env <dev|prod> / --all; --sweep and --keep-images are optional.
    parser = argparse.ArgumentParser(description="Find (and with --sweep remove) ShopAssist leftovers.")
    parser.add_argument("--region", required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--env", choices=["dev", "prod"], help="one environment")
    scope.add_argument("--all", action="store_true", help="every environment, the platform and the state bucket")
    parser.add_argument("--sweep", action="store_true", help="remove what outlives terraform destroy")
    parser.add_argument("--keep-images", action="store_true", help="with --sweep: don't delete ECR images")
    args = parser.parse_args()

    # env = None means "everything" (--all). One AWS session for every call; `c` is a shortcut for
    # creating a client of one service. `found` collects leftovers per service, `problems` the checks
    # that couldn't run.
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
        # 2a. ECR images: every image in every shopassist-* repository, deleted 100 at a time (the
        # most one call accepts). The repositories stay; they belong to the platform stack.
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
        # 2b. AgentCore Identity secrets of this scope, including ones already scheduled for deletion
        # (IncludePlannedDeletion). The scope is judged on the last part of the name (after the last "/").
        sm = c("secretsmanager")
        for s in paged(sm, "list_secrets", "SecretList", IncludePlannedDeletion=True):
            if "bedrock-agentcore-identity" in s["Name"] and matches(s["Name"].split("/")[-1], pats, env):
                sm.delete_secret(SecretId=s["ARN"], ForceDeleteWithoutRecovery=True)
                print(f"  swept secret {s['Name']} (no recovery window)")
        # 2c. Every log group whose name matches the scope (log messages cost money while stored).
        logs = c("logs")
        for g in paged(logs, "describe_log_groups", "logGroups"):
            if matches(g["logGroupName"], pats, env):
                logs.delete_log_group(logGroupName=g["logGroupName"])
                print(f"  swept log group {g['logGroupName']}")
        # 2d. One environment only: its state file. Already gone (or no bucket at all) is fine.
        if env is not None:
            bucket = f"{PROJECT}-tfstate-{account}-{region}"
            try:
                c("s3").delete_object(Bucket=bucket, Key=f"main/{env}.tfstate")
                print(f"  swept state file s3://{bucket}/main/{env}.tfstate")
            except ClientError as e:
                if e.response["Error"]["Code"] not in ("NoSuchBucket", "NoSuchKey"):
                    problems.append(f"state file: {e.response['Error']['Code']}")

    # ---------------------------------------------------------------- Step 3: scan
    # 3a. One list call per service, returning resource names; only names in the scope are kept.
    # Each entry is wrapped in `safe`, so one denied call doesn't stop the rest of the scan.
    # Budgets is a global service that is always called in us-east-1.
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

    # 3b. ECR: with --all the repositories themselves are leftovers; for one env only images, and only when sweeping them.
    def ecr_check() -> list[str]:
        """List shopassist-* repositories (with --all) or images that a sweep should have removed."""
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

    def still_exists(arn: str) -> bool:
        """The tagging index can list a resource for hours after it was deleted. Ask the service itself
        where that's cheap; anything we can't confirm is treated as existing (safe side)."""
        try:
            if ":cognito-idp:" in arn and ":userpool/" in arn:
                c("cognito-idp").describe_user_pool(UserPoolId=arn.rsplit("/", 1)[1])
            elif ":dynamodb:" in arn and ":table/" in arn:
                c("dynamodb").describe_table(TableName=arn.rsplit("/", 1)[1])
            elif ":lambda:" in arn and ":function:" in arn:
                c("lambda").get_function(FunctionName=arn.rsplit(":", 1)[1])
            elif arn.startswith("arn:aws:s3:::"):
                c("s3").head_bucket(Bucket=arn.split(":::", 1)[1])
            elif ":logs:" in arn and ":log-group:" in arn:
                name = arn.split(":log-group:", 1)[1].removesuffix(":*")
                return any(g["logGroupName"] == name for g in c("logs").describe_log_groups(logGroupNamePrefix=name)["logGroups"])
            return True
        except ClientError as e:
            code = e.response["Error"]["Code"]
            return code not in ("ResourceNotFoundException", "NotFound", "404", "NoSuchBucket")

    # 3c. The tagging API catches resources the name checks above don't cover. ARNs (Amazon Resource
    # Names, AWS's full ids) that only the lagging tag index still lists go to `stale` (a NOTE, not a
    # failure).
    stale: list[str] = []

    def tagged() -> list[str]:
        """ARNs tagged project=shopassist (and environment=<env> for one env) that really still exist."""
        filters = [{"Key": "project", "Values": [PROJECT]}]
        if env is not None:
            filters.append({"Key": "environment", "Values": [env]})
        arns = [r["ResourceARN"] for r in paged(c("resourcegroupstaggingapi"), "get_resources", "ResourceTagMappingList", TagFilters=filters)]
        # Skip ARNs already explained by the name checks above, then confirm the rest really exist.
        arns = [a for a in arns if not any(n.split(" ")[0] in a for hits in found.values() for n in hits)]
        live = [a for a in arns if still_exists(a)]
        stale.extend(a for a in arns if a not in live)
        return live
    tag_hits = safe("Tagging API", tagged, problems)
    if tag_hits:
        found["Tagged (project=shopassist)"] = tag_hits

    # 3d. With --all: the GitHub OIDC (OpenID Connect) provider, which has no ShopAssist name or tag.
    if env is None:
        try:
            c("iam").get_open_id_connect_provider(
                OpenIDConnectProviderArn=f"arn:aws:iam::{account}:oidc-provider/token.actions.githubusercontent.com")
            found["IAM OIDC provider"] = ["token.actions.githubusercontent.com (created by the platform stack)"]
        except ClientError:
            pass

    # ---------------------------------------------------------------- Step 4: report
    for a in stale:
        print(f"NOTE  already deleted, still in AWS's tag index (it catches up later): {a}")
    if not found and not problems:
        print("CLEAN: no ShopAssist resources left in this scope.")
        return 0
    for label, hits in found.items():
        print(f"LEFT  {label}:")
        for h in hits:
            print(f"        {h}")
    for p in problems:
        print(f"WARN  {p}")
    # Only real leftovers fail the run; checks that couldn't run are warnings.
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
