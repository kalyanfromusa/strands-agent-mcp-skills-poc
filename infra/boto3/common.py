"""Shared plumbing for the boto3 deployment: names, tags, state, progress output, waiting and retries.

What this file does
    Holds the helpers that every deploy step needs: the names of all AWS resources, a small "memory" file
    (`state.json`) that records what was created, progress printing, polling ("wait until it is ready"),
    and retry rules for a common AWS timing problem with new permissions.

Where it fits
    `deploy.py` and `destroy.py` build one `Context` with `make_context()` and hand it to each step module
    in `steps/`. Nothing in here talks to a specific AWS service. The step modules hold the actual API
    calls, so they can be read top to bottom as "the raw calls in dependency order".

Step by step (what a step module typically does with these helpers)
    1. Read a resource name from `ctx.names` (for example `ctx.names.gateway` -> "shopassist-gateway").
    2. Get an AWS client with `ctx.client("service")` (created once, then reused).
    3. Look the resource up. If it is missing, create it. If it exists, update it only when needed.
       This "get-or-create-or-update" pattern is what makes re-running the deploy safe (idempotent).
    4. If AWS needs time to finish, poll with `wait_for_status()` until the status is READY/ACTIVE.
    5. Save the new IDs and addresses with `ctx.state.put(...)`, which writes `state.json` straight away.

Key terms
    boto3: the official Python library for calling Amazon Web Services (AWS) APIs.
    ARN (Amazon Resource Name): the unique address of one AWS resource, e.g. arn:aws:lambda:...:function:x.
    Prefix: a short word (default "shopassist") put in front of every resource name and used as a tag.
    Tag: a label (key/value) attached to a resource, here {"project": "<prefix>"}, for cost tracking.
    Idempotent: running the same step twice gives the same result as running it once.
    IAM (Identity and Access Management): the AWS service for permissions (roles and policies).
    Eventually consistent: a change is saved but takes a few seconds to be visible everywhere in AWS.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import boto3
from botocore.exceptions import ClientError

# Important file and folder locations, all worked out relative to this file so the scripts can be run
# from any current directory. `state.json` sits next to this file and is git-ignored.
REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = Path(__file__).resolve().parent / "state.json"
DEPLOY_DIR = REPO_ROOT / ".deploy"
DIST_DIR = REPO_ROOT / "dist"
SKILLS_DIR = REPO_ROOT / "skills"
POLICIES_DIR = REPO_ROOT / "infra" / "policies"
TOOLS_SCHEMA_PATH = REPO_ROOT / "mcp_servers" / "orders_lambda" / "tools_schema.json"
SEED_SCRIPT = REPO_ROOT / "scripts" / "seed_data.py"

# The three code packages that get uploaded to S3. They must be built first with scripts/build.py.
ARTIFACTS = {  # logical name -> zip produced by scripts/build.py
    "agent": DIST_DIR / "agent.zip",
    "support_mcp": DIST_DIR / "support_mcp.zip",
    "orders_lambda": DIST_DIR / "orders_lambda.zip",
}

# Defaults used when the command line does not say otherwise.
DEFAULT_REGION = "us-east-1"
DEFAULT_PREFIX = "shopassist"
DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-5-5"
# Logs are kept for 7 days only, to keep the PoC cheap (the AWS default is "keep forever").
LOG_RETENTION_DAYS = 7

# Same rule as the Terraform `name_prefix` validation. Lowercase letters and digits are the only
# characters valid in every name we derive (S3, Cognito domain, AgentCore runtime names).
PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9]{1,20}$")


# --------------------------------------------------------------------------------------- names


@dataclass(frozen=True)
class Names:
    """Every resource name, derived from the prefix (design.md section 1).

    Keeping all names in one place means deploy and destroy always agree on what to look for, and the
    boto3 path produces the same names as the Terraform path. Each property below returns one name.
    """

    prefix: str
    account_id: str
    region: str

    @property
    def tags(self) -> dict[str, str]:
        """The label put on every resource, so costs and resources can be filtered by project."""
        return {"project": self.prefix}

    # S3 (file storage). Bucket names are global across all AWS customers, so the account number and
    # region are added to make the name unique.
    @property
    def bucket(self) -> str:
        return f"{self.prefix}-artifacts-{self.account_id}-{self.region}"

    # DynamoDB (the database tables holding the demo store data)
    @property
    def orders_table(self) -> str:
        return f"{self.prefix}-orders"

    @property
    def returns_table(self) -> str:
        return f"{self.prefix}-returns"

    @property
    def tickets_table(self) -> str:
        return f"{self.prefix}-tickets"

    # Cognito (user directory and login service)
    @property
    def user_pool(self) -> str:
        return f"{self.prefix}-pool"

    @property
    def cognito_domain(self) -> str:
        """Web address prefix of the login service. It must be unique worldwide, hence the account number."""
        return f"{self.prefix}-{self.account_id}"

    @property
    def resource_server_id(self) -> str:
        return self.prefix

    @property
    def scope(self) -> str:
        """The permission name machine logins ask for, e.g. "shopassist/invoke"."""
        return f"{self.prefix}/invoke"

    @property
    def m2m_client(self) -> str:
        """App client for machine-to-machine (M2M) logins: a program logs in, not a person."""
        return f"{self.prefix}-m2m"

    @property
    def users_client(self) -> str:
        """App client the demo users (alice, bob, carol) sign in with."""
        return f"{self.prefix}-users"

    @property
    def discovery_url_template(self) -> str:
        """Standard OpenID Connect "discovery" address of the user pool.

        AWS services read this document to learn how to check Cognito tokens. The pool ID is only known
        after the pool exists, so `{pool_id}` is filled in later.
        """
        return f"https://cognito-idp.{self.region}.amazonaws.com/{{pool_id}}/.well-known/openid-configuration"

    @property
    def token_url(self) -> str:
        """Address where programs exchange their client ID and secret for a login token."""
        return f"https://{self.cognito_domain}.auth.{self.region}.amazoncognito.com/oauth2/token"

    # AgentCore Identity (safe storage for the machine login secret)
    @property
    def oauth_provider(self) -> str:
        return f"{self.prefix}-cognito"

    # Lambda (the small on-demand function that implements the orders tools)
    @property
    def lambda_function(self) -> str:
        return f"{self.prefix}-orders-tools"

    @property
    def lambda_arn(self) -> str:
        """The function's address can be written down in advance, which the Gateway role needs early."""
        return f"arn:aws:lambda:{self.region}:{self.account_id}:function:{self.lambda_function}"

    # AgentCore Runtime / Gateway / Policy (runtime and policy names: [a-zA-Z][a-zA-Z0-9_]*)
    # These names use underscores because AgentCore Runtime and Policy names may not contain dashes.
    @property
    def agent_runtime(self) -> str:
        return f"{self.prefix}_agent"

    @property
    def mcp_runtime(self) -> str:
        return f"{self.prefix}_support_mcp"

    @property
    def gateway(self) -> str:
        return f"{self.prefix}-gateway"

    @property
    def policy_engine(self) -> str:
        return f"{self.prefix}_policy_engine"

    # IAM. Same role names as the Terraform modules, so both paths produce identical resources.
    # An IAM role is a set of permissions that one AWS service is allowed to use on our behalf.
    ROLE_BASENAMES = {
        "lambda": "orders-tools",          # Terraform: "${function_name}-role"
        "mcp-runtime": "support-mcp-runtime",
        "gateway": "gateway",              # Terraform: "${gateway_name}-role"
        "agent-runtime": "agent-runtime",
    }

    def role_basename(self, kind: str) -> str:
        """Short name of a role kind, also used as the name of the role's permission policy."""
        return self.ROLE_BASENAMES[kind]

    def role(self, kind: str) -> str:
        """Full role name, e.g. role("gateway") -> "shopassist-gateway-role"."""
        return f"{self.prefix}-{self.ROLE_BASENAMES[kind]}-role"

    # Budget (optional monthly cost alarm)
    @property
    def budget(self) -> str:
        return f"{self.prefix}-monthly"

    # ARN helpers: build resource addresses used inside permission policies.
    def table_arn(self, table: str) -> str:
        """Address of a DynamoDB table, used to grant access to exactly that table."""
        return f"arn:aws:dynamodb:{self.region}:{self.account_id}:table/{table}"

    def agentcore_arn(self, resource: str) -> str:
        """Address of an AgentCore resource (gateway, policy engine, token vault...) in this account."""
        return f"arn:aws:bedrock-agentcore:{self.region}:{self.account_id}:{resource}"


def tag_list(tags: dict[str, str], key: str = "Key", value: str = "Value") -> list[dict[str, str]]:
    """Most older AWS APIs want tags as [{"Key": k, "Value": v}] instead of a map."""
    return [{key: k, value: v} for k, v in tags.items()]


# --------------------------------------------------------------------------------------- state


class State:
    """`infra/boto3/state.json`: IDs/ARNs of everything created, saved after every change.

    A crashed deploy can be re-run (every step is get-or-create anyway) and `destroy.py` uses the
    state to find resources quickly. Destroy also falls back to name lookups, so a lost state file
    is not fatal.

    Shape of the file: {"region", "prefix", "account_id", "resources": {"s3": {...}, "cognito": {...}, ...}}.
    Each step writes its own "section" under "resources", and later steps read earlier sections
    (for example the Gateway step reads the Lambda ARN from the "lambda" section).
    """

    def __init__(self, path: Path = STATE_PATH):
        """Load the previous state file if there is one, otherwise start empty."""
        self.path = path
        self.data: dict[str, Any] = {}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def section(self, name: str) -> dict[str, Any]:
        """Return one step's saved values (creating an empty section the first time)."""
        return self.data.setdefault("resources", {}).setdefault(name, {})

    def put(self, section: str, /, **values: Any) -> None:
        """Record new values for a step and save to disk at once, so a crash loses nothing."""
        # positional-only, so a value may itself be called `name` (e.g. put("identity", name=...))
        self.section(section).update(values)
        self.save()

    def drop(self, name: str) -> None:
        """Forget a step's values after its resources have been deleted."""
        self.data.get("resources", {}).pop(name, None)
        self.save()

    def save(self) -> None:
        """Write the file safely: write a temporary file first, then swap it in.

        If the program is stopped halfway through writing, the old state.json is still intact.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.path)


# --------------------------------------------------------------------------------------- context


@dataclass
class Context:
    """Everything a step needs, passed to every step function as `ctx`.

    It carries the target region, prefix, AWS account, the login session to AWS, the state file, the
    chosen model, and `flags` (small messages from one step to a later one in the same run).
    """

    region: str
    prefix: str
    account_id: str
    session: boto3.Session
    state: State
    model_id: str = DEFAULT_MODEL_ID
    flags: dict[str, Any] = field(default_factory=dict)  # cross-step signals, e.g. "mcp_runtime_updated"
    _clients: dict[str, Any] = field(default_factory=dict)

    @property
    def names(self) -> Names:
        """All resource names for this prefix, account and region."""
        return Names(self.prefix, self.account_id, self.region)

    def client(self, service: str, region: str | None = None):
        """Return a boto3 client for one AWS service, created once and then reused.

        `region` is only passed for global services that live in a fixed region (AWS Budgets).
        """
        key = f"{service}@{region or self.region}"
        if key not in self._clients:
            self._clients[key] = self.session.client(service, region_name=region or self.region)
        return self._clients[key]


def make_context(region: str, prefix: str, model_id: str = DEFAULT_MODEL_ID) -> Context:
    """Connect to AWS, find out which account we are in, and load the state file.

    Step 1: open an AWS session with the credentials of whoever runs the script.
    Step 2: ask STS (Security Token Service) "who am I?" to get the account number, which is part of
            several resource names.
    Step 3: load state.json and stamp it with the region, prefix and account of this run.
    """
    session = boto3.Session(region_name=region)
    # AWS API call: GetCallerIdentity returns the account ID of the current credentials. It needs no
    # special permissions, so it is also a quick check that the AWS login works at all.
    account_id = session.client("sts").get_caller_identity()["Account"]
    state = State()
    state.data.update({"region": region, "prefix": prefix, "account_id": account_id})
    return Context(region=region, prefix=prefix, account_id=account_id, session=session,
                   state=state, model_id=model_id)


# --------------------------------------------------------------------------------------- output


def _symbol(preferred: str, fallback: str) -> str:
    """Use ✓/… when the console can print them (Windows code pages often can't)."""
    try:
        preferred.encode(sys.stdout.encoding or "ascii")
        return preferred
    except (UnicodeEncodeError, LookupError):
        return fallback


# The tick, dots and cross symbols used in progress output (plain text on consoles that can't show them).
OK = _symbol("✓", "OK")
DOTS = _symbol("…", "...")
CROSS = _symbol("✗", "X")


class Progress:
    """Prints `[n/total] title`, then indented detail lines, then `✓ title (12.3s)`."""

    def __init__(self, total: int):
        """Remember how many steps there are and when the whole run started."""
        self.total = total
        self.n = 0
        self.started = time.monotonic()

    def step(self, title: str) -> "_Step":
        """Start the next numbered step. Used as `with progress.step("..."):`."""
        self.n += 1
        return _Step(f"[{self.n}/{self.total}] {title}")

    def elapsed(self) -> str:
        """Total time since the run started, e.g. "4m12s"."""
        return fmt_seconds(time.monotonic() - self.started)


class _Step:
    """One step's header and footer: prints the title on entry and "done"/"failed" with the time on exit."""

    def __init__(self, title: str):
        self.title = title

    def __enter__(self) -> "_Step":
        self.t0 = time.monotonic()
        print(f"\n{self.title} {DOTS}", flush=True)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Does not swallow errors: after printing "failed", the error carries on to the caller.
        took = fmt_seconds(time.monotonic() - self.t0)
        if exc_type is None:
            print(f"  {OK} done ({took})", flush=True)
        else:
            print(f"  {CROSS} failed after {took}: {exc}", flush=True)


def info(msg: str) -> None:
    """Print one indented detail line under the current step."""
    print(f"    {msg}", flush=True)


def fmt_seconds(seconds: float) -> str:
    """Readable duration: "12.3s" under 90 seconds, otherwise "4m05s"."""
    return f"{seconds:.1f}s" if seconds < 90 else f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


# --------------------------------------------------------------------------------------- errors


# Different AWS services use different error codes for "that thing does not exist". This list collects
# them, so every step can treat "not found" the same way.
NOT_FOUND_CODES = {
    "ResourceNotFoundException", "NoSuchEntity", "NotFoundException", "NoSuchBucket", "NoSuchKey",
    "404", "NotFound", "UserNotFoundException", "ResourceNotFound",
}


def error_code(err: ClientError) -> str:
    """The short error code AWS sent back, e.g. "AccessDeniedException"."""
    return err.response.get("Error", {}).get("Code", "")


def is_not_found(err: Exception) -> bool:
    """True when an AWS error means "this resource does not exist".

    Steps use this to tell "missing, so create it" (or "already deleted, fine") apart from real errors,
    which must still stop the run.
    """
    if not isinstance(err, ClientError):
        return False
    code = error_code(err)
    if code in NOT_FOUND_CODES:
        return True
    # Cognito reports a missing pool/domain as ResourceNotFound; some APIs use ValidationException + message.
    msg = str(err).lower()
    return code == "ValidationException" and ("not found" in msg or "does not exist" in msg)


def ignore_not_found(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a delete call and treat "already gone" as success (destroy must be re-runnable).

    Returns the AWS response, or None when the resource was already gone. Callers use that None to
    print "already gone" instead of "deleted".
    """
    try:
        return fn(*args, **kwargs)
    except ClientError as err:
        if is_not_found(err):
            return None
        raise


# IAM is eventually consistent: a role created a few seconds ago may not be assumable yet. Services
# report that as AccessDenied / ValidationException / InvalidParameterValue with a message about
# the role. Retry those for a while; anything else fails immediately.
_IAM_RETRY_CODES = {"AccessDeniedException", "ValidationException", "InvalidParameterValueException",
                    "InvalidParameterException"}
_IAM_RETRY_HINTS = ("role", "assume", "not authorized", "permission", "trust")


def call_with_iam_retry(fn: Callable[..., Any], *, timeout: float = 120, **kwargs: Any) -> Any:
    """Call an AWS create/update API, retrying while a brand-new IAM role is not usable yet.

    Why: when a new role is created, AWS takes a few seconds (sometimes up to a minute) to make it
    usable everywhere. A service that tries to use the role too early refuses. Instead of failing the
    deploy, we wait and try again: 5s, then 7.5s, ~11s, then every 15s, for up to `timeout` seconds.
    Only errors that look like this timing problem are retried; any other error stops at once.
    """
    deadline = time.monotonic() + timeout
    delay = 5.0
    while True:
        try:
            return fn(**kwargs)
        except ClientError as err:
            # Step 1: decide whether this is the "role not ready yet" problem (code AND message must match).
            msg = str(err).lower()
            retryable = error_code(err) in _IAM_RETRY_CODES and any(h in msg for h in _IAM_RETRY_HINTS)
            # Step 2: give up if it is a different error, or we have waited long enough.
            if not retryable or time.monotonic() > deadline:
                raise
            # Step 3: wait a bit longer each time (capped at 15 seconds), then try again.
            info(f"{DOTS} IAM role not usable yet ({error_code(err)}), retrying in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 1.5, 15)


# --------------------------------------------------------------------------------------- waiting


class WaitFailed(RuntimeError):
    """Raised when a resource ends up in a failed state, or does not become ready in time."""

    pass


def wait_for_status(
    what: str,
    get_status: Callable[[], tuple[str | None, str]],
    *,
    ready: Iterable[str],
    failed: Iterable[str] = (),
    timeout: float = 600,
    interval: float = 5,
) -> str:
    """Poll `get_status()` -> (status, detail) until it reaches a ready state.

    status None means "resource is gone", which counts as ready when "DELETED" is in `ready`.
    Prints every status change and a heartbeat every ~30s so a long wait never looks hung.

    Why polling: many AWS create calls return immediately with a status like CREATING, and the real
    work carries on in the background. The next step must not start until the resource is READY or
    ACTIVE, so we ask again every `interval` seconds.
    """
    ready, failed = set(ready), set(failed)
    t0 = time.monotonic()
    last_status, last_print = "", 0.0
    while True:
        # Step 1: ask AWS for the current status (a missing resource counts as "DELETED").
        status, detail = get_status()
        status = status or "DELETED"
        elapsed = time.monotonic() - t0
        # Step 2: finished successfully?
        if status in ready:
            info(f"{OK} {what}: {status} ({fmt_seconds(elapsed)})")
            return status
        # Step 3: failed? Stop and show AWS's reason.
        if status in failed:
            raise WaitFailed(f"{what} reached {status}: {detail or 'no reason given'}")
        # Step 4: still in progress. Print on change, or every 30 seconds, so the user sees it is alive.
        if status != last_status or elapsed - last_print >= 30:
            info(f"{DOTS} {what}: {status} ({fmt_seconds(elapsed)})")
            last_status, last_print = status, elapsed
        # Step 5: give up after `timeout` seconds, otherwise sleep and ask again.
        if elapsed > timeout:
            raise WaitFailed(f"timed out after {fmt_seconds(elapsed)} waiting for {what} (last status {status})")
        time.sleep(interval)


# --------------------------------------------------------------------------------------- misc


def paginate(fn: Callable[..., dict], list_key: str, token_in: str = "nextToken",
             token_out: str = "nextToken", **kwargs: Any) -> Iterable[dict]:
    """Generic pagination for APIs without a botocore paginator (most AgentCore List* calls).

    AWS "List" calls return results one page at a time plus a "next page" token. This keeps asking
    for the next page until there is no token left, and hands back the items one by one.
    """
    while True:
        page = fn(**kwargs)
        yield from page.get(list_key, [])
        token = page.get(token_out)
        if not token:
            return
        kwargs[token_in] = token


def policy_json(statements: list[dict]) -> str:
    """Wrap permission statements in the standard IAM policy document format (as JSON text)."""
    return json.dumps({"Version": "2012-10-17", "Statement": statements})
