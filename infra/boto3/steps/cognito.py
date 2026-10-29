"""Amazon Cognito: the OAuth2 authorization server for every JWT in the system.

What this file does
    Deploy step 2. Sets up the user directory and login service: the demo users, their groups, and the
    "app clients" (registered programs) that are allowed to ask for login tokens.

Where it fits
    Every later security check reads tokens issued here: the agent runtime (who may use the agent), the
    Gateway (who may call tools) and the support MCP runtime (only the Gateway may call it). Step 3
    (`identity.py`) stores the machine client's secret, and `scripts/invoke_agent.py` signs users in.

* **User pool** `<prefix>-pool` with a **domain** `<prefix>-<account>`: the domain gives the pool an
  OAuth2 token endpoint (`https://<domain>.auth.<region>.amazoncognito.com/oauth2/token`), and it is
  what makes `token_endpoint` appear in the pool's OIDC discovery document, which AgentCore reads.
* **Resource server** `<prefix>` with scope `invoke` -> full scope `<prefix>/invoke`. Client-credentials
  tokens must ask for a custom scope, so M2M auth needs this.
* **M2M app client** `<prefix>-m2m` (client_credentials + secret). Used by the Gateway to call the
  support MCP runtime (outbound OAuth via AgentCore Identity) and for local development.
* **Users app client** `<prefix>-users` (public, no secret, USER_PASSWORD_AUTH). Demo users sign in
  with it; their access token carries `cognito:groups`, which the agent runtime authorizer and the
  Gateway Cedar policies check (design.md section 8).
* **Groups** `shop-admin`, `shop-viewer` and **users** alice (admin), bob (viewer), carol (no group).
  Passwords are random and written to `.deploy/users.json` (git-ignored), and reused on re-deploys.

Step by step
    1. User pool (the directory itself).
    2. Domain (the web address of the login service), then wait until it is ACTIVE.
    3. Resource server and its `invoke` scope (a named permission machine logins ask for).
    4. App clients: `-m2m` for programs, `-users` for people.
    5. Groups `shop-admin` and `shop-viewer`.
    6. Demo users alice, bob, carol with passwords, added to their groups.

Key terms
    User pool: Cognito's user directory (usernames, passwords, groups).
    App client: a program registered with the pool that may request tokens; it has an ID (and maybe a secret).
    Scope: a named permission written into a token, here `<prefix>/invoke`.
    client_credentials: the OAuth2 login type for programs (ID + secret, no person).
    OIDC (OpenID Connect) discovery document: a public web page describing how to check the pool's tokens.
    Access token: the JWT a user or program gets after logging in, sent with every request.
"""

from __future__ import annotations

import json
import secrets
import string

from common import DEPLOY_DIR, Context, OK, ignore_not_found, info, is_not_found, wait_for_status

# Where the demo users' generated passwords are written (git-ignored, never committed).
USERS_FILE = DEPLOY_DIR / "users.json"
# Group name -> description. The Cedar policies give shop-admin all tools and shop-viewer read-only tools.
GROUPS = {"shop-admin": "All tools", "shop-viewer": "Read-only tools"}
# Demo user -> group. carol is in no group, so the agent runtime turns her away (HTTP 403).
DEMO_USERS = {"alice": "shop-admin", "bob": "shop-viewer", "carol": ""}  # "" = no group (same as Terraform)

# Password rules for the user pool. Generated passwords always meet them.
PASSWORD_POLICY = {
    "MinimumLength": 12, "RequireUppercase": True, "RequireLowercase": True,
    "RequireNumbers": True, "RequireSymbols": True, "TemporaryPasswordValidityDays": 1,
}


# ------------------------------------------------------------------ request builders (pure)


def build_create_user_pool_request(pool_name: str, tags: dict[str, str]) -> dict:
    """CreateUserPool request: an admin-managed directory (nobody can sign themselves up)."""
    return {
        "PoolName": pool_name,
        "Policies": {"PasswordPolicy": PASSWORD_POLICY},
        "AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True},  # no self sign-up
        "UsernameConfiguration": {"CaseSensitive": False},
        "DeletionProtection": "INACTIVE",  # PoC: destroy must be able to delete it
        "UserPoolTags": tags,
    }


def build_create_domain_request(domain: str, pool_id: str) -> dict:
    """CreateUserPoolDomain request: gives the pool its login web address (and its token endpoint)."""
    return {"Domain": domain, "UserPoolId": pool_id}


def build_resource_server_request(pool_id: str, identifier: str) -> dict:
    """Create/UpdateResourceServer request: defines the `invoke` scope that machine tokens ask for."""
    return {
        "UserPoolId": pool_id,
        "Identifier": identifier,
        "Name": f"{identifier}-api",
        "Scopes": [{"ScopeName": "invoke", "ScopeDescription": "Call ShopAssist MCP tools"}],
    }


def build_m2m_client_request(pool_id: str, client_name: str, scope: str) -> dict:
    """CreateUserPoolClient request for the machine client `<prefix>-m2m`.

    GenerateSecret: it gets a client secret (a password for programs).
    AllowedOAuthFlows client_credentials: it logs in with ID + secret, no user involved.
    AllowedOAuthScopes: the only permission it may ask for is `<prefix>/invoke`.
    Tokens are valid for 60 minutes.
    """
    return {
        "UserPoolId": pool_id,
        "ClientName": client_name,
        "GenerateSecret": True,
        "AllowedOAuthFlows": ["client_credentials"],
        "AllowedOAuthScopes": [scope],
        "AllowedOAuthFlowsUserPoolClient": True,
        "SupportedIdentityProviders": ["COGNITO"],
        "ExplicitAuthFlows": ["ALLOW_REFRESH_TOKEN_AUTH"],
        "AccessTokenValidity": 60,
        "TokenValidityUnits": {"AccessToken": "minutes"},
    }


def build_users_client_request(pool_id: str, client_name: str) -> dict:
    """CreateUserPoolClient request for the people client `<prefix>-users`.

    No secret, because a command-line tool cannot keep one safe. ALLOW_USER_PASSWORD_AUTH lets the CLI
    send username + password directly. PreventUserExistenceErrors hides whether a username exists.
    """
    return {
        "UserPoolId": pool_id,
        "ClientName": client_name,
        "GenerateSecret": False,  # public client: the CLI signs users in directly
        "ExplicitAuthFlows": ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
        "PreventUserExistenceErrors": "ENABLED",
        "AccessTokenValidity": 60,
        "TokenValidityUnits": {"AccessToken": "minutes"},
    }


def as_update_client_request(create_request: dict, client_id: str) -> dict:
    """UpdateUserPoolClient takes the same settings minus GenerateSecret (a secret can't be added later)."""
    req = {k: v for k, v in create_request.items() if k != "GenerateSecret"}
    req["ClientId"] = client_id
    return req


def build_create_group_request(pool_id: str, group: str, description: str) -> dict:
    """CreateGroup request for one group (shop-admin or shop-viewer)."""
    return {"UserPoolId": pool_id, "GroupName": group, "Description": description}


def build_admin_create_user_request(pool_id: str, username: str) -> dict:
    """AdminCreateUser request. MessageAction SUPPRESS: don't send a welcome email (the users are fictional)."""
    return {"UserPoolId": pool_id, "Username": username, "MessageAction": "SUPPRESS"}


def build_set_password_request(pool_id: str, username: str, password: str) -> dict:
    """AdminSetUserPassword request. Permanent: the user is not forced to change it at first sign-in."""
    return {"UserPoolId": pool_id, "Username": username, "Password": password, "Permanent": True}


def generate_password(length: int = 20) -> str:
    """Random password that satisfies PASSWORD_POLICY (one of each class guaranteed)."""
    symbols = "!#%*+-=_"
    pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, symbols]
    # Step 1: one character from each required class, then fill up with random ones.
    chars = [secrets.choice(p) for p in pools]
    alphabet = "".join(pools)
    chars += [secrets.choice(alphabet) for _ in range(length - len(chars))]
    # Step 2: shuffle, so the guaranteed characters are not always at the start.
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


# ------------------------------------------------------------------ lookups


def find_user_pool(idp, name: str) -> str | None:
    """Find a user pool ID by name (AWS API ListUserPools, page by page). None if there is none."""
    for page in idp.get_paginator("list_user_pools").paginate(MaxResults=60):
        for pool in page["UserPools"]:
            if pool["Name"] == name:
                return pool["Id"]
    return None


def find_client(idp, pool_id: str, name: str) -> str | None:
    """Find an app client ID by name (AWS API ListUserPoolClients). None if there is none."""
    for page in idp.get_paginator("list_user_pool_clients").paginate(UserPoolId=pool_id, MaxResults=60):
        for client in page["UserPoolClients"]:
            if client["ClientName"] == name:
                return client["ClientId"]
    return None


def _ensure_client(idp, pool_id: str, create_req: dict) -> str:
    """Create the app client, or re-apply its settings if it already exists. Returns the client ID.

    Updating (instead of re-creating) keeps the same client ID and secret, which other resources
    (credential provider, Gateway, runtimes) already point at.
    """
    client_id = find_client(idp, pool_id, create_req["ClientName"])
    if client_id:
        # AWS API call: UpdateUserPoolClient overwrites the client's settings with the desired ones.
        idp.update_user_pool_client(**as_update_client_request(create_req, client_id))
        info(f"app client {create_req['ClientName']} exists ({client_id}), settings refreshed")
    else:
        # AWS API call: CreateUserPoolClient registers a new client and returns its ID.
        client_id = idp.create_user_pool_client(**create_req)["UserPoolClient"]["ClientId"]
        info(f"{OK} created app client {create_req['ClientName']} ({client_id})")
    return client_id


# ------------------------------------------------------------------ deploy / destroy


def deploy(ctx: Context) -> None:
    """Create or refresh the whole Cognito setup (pool, domain, scope, clients, groups, users)."""
    idp = ctx.client("cognito-idp")
    n = ctx.names

    # 1. User pool
    # Look it up by name first; only create it (AWS API CreateUserPool) when it is missing.
    pool_id = find_user_pool(idp, n.user_pool)
    if pool_id:
        info(f"user pool {n.user_pool} exists ({pool_id})")
    else:
        pool_id = idp.create_user_pool(**build_create_user_pool_request(n.user_pool, n.tags))["UserPool"]["Id"]
        info(f"{OK} created user pool {n.user_pool} ({pool_id})")
    discovery_url = n.discovery_url_template.format(pool_id=pool_id)
    ctx.state.put("cognito", user_pool_id=pool_id, discovery_url=discovery_url)

    # 2. Domain (globally unique name; must exist before the OAuth2 credential provider reads discovery)
    # AWS API DescribeUserPoolDomain returns an empty description (not an error) when the domain is free.
    # If the name is taken by a different pool (e.g. an old one), stop with a clear message.
    existing = idp.describe_user_pool_domain(Domain=n.cognito_domain).get("DomainDescription", {})
    if existing.get("UserPoolId") and existing["UserPoolId"] != pool_id:
        raise RuntimeError(f"Cognito domain {n.cognito_domain} belongs to another pool ({existing['UserPoolId']}).")
    if not existing.get("UserPoolId"):
        idp.create_user_pool_domain(**build_create_domain_request(n.cognito_domain, pool_id))
        info(f"{OK} created domain {n.cognito_domain}")
    # A new domain takes a little while to go live; wait until its status is ACTIVE.
    wait_for_status(
        f"domain {n.cognito_domain}",
        lambda: (idp.describe_user_pool_domain(Domain=n.cognito_domain)["DomainDescription"].get("Status"), ""),
        ready={"ACTIVE"}, failed={"FAILED"}, timeout=300,
    )
    ctx.state.put("cognito", domain=n.cognito_domain, token_url=n.token_url)

    # 3. Resource server + scope
    # Describe it: if it exists, update it (so the scope list is current); if "not found", create it.
    rs_req = build_resource_server_request(pool_id, n.resource_server_id)
    try:
        idp.describe_resource_server(UserPoolId=pool_id, Identifier=n.resource_server_id)
        idp.update_resource_server(**rs_req)
        info(f"resource server {n.resource_server_id} exists, scopes refreshed")
    except Exception as err:  # noqa: BLE001
        if not is_not_found(err):
            raise
        idp.create_resource_server(**rs_req)
        info(f"{OK} created resource server {n.resource_server_id} (scope {n.scope})")

    # 4. App clients
    # The M2M client needs the scope from step 3, so it must come after it.
    m2m_id = _ensure_client(idp, pool_id, build_m2m_client_request(pool_id, n.m2m_client, n.scope))
    users_id = _ensure_client(idp, pool_id, build_users_client_request(pool_id, n.users_client))
    # The M2M client secret is NOT stored in state.json; identity.py reads it from Cognito when needed.
    ctx.state.put("cognito", m2m_client_id=m2m_id, users_client_id=users_id)

    # 5. Groups
    # AWS API CreateGroup. "Group already exists" is fine on a re-run; any other error stops the deploy.
    for group, description in GROUPS.items():
        try:
            idp.create_group(**build_create_group_request(pool_id, group, description))
            info(f"{OK} created group {group}")
        except Exception as err:  # noqa: BLE001
            if "GroupExistsException" not in str(err):
                raise

    # 6. Demo users (passwords reused from .deploy/users.json when present)
    # Reusing passwords means a re-deploy does not invalidate the credentials you already have.
    users = json.loads(USERS_FILE.read_text(encoding="utf-8")) if USERS_FILE.exists() else {}
    for username, group in DEMO_USERS.items():
        password = users.get(username, {}).get("password") or generate_password()
        # AWS API AdminCreateUser: create the user as an administrator ("already exists" is fine).
        try:
            idp.admin_create_user(**build_admin_create_user_request(pool_id, username))
            info(f"{OK} created user {username}")
        except Exception as err:  # noqa: BLE001
            if "UsernameExistsException" not in str(err):
                raise
        # AWS API AdminSetUserPassword: set a permanent password, so the user can sign in straight away.
        idp.admin_set_user_password(**build_set_password_request(pool_id, username, password))
        # AWS API AdminAddUserToGroup: put alice in shop-admin and bob in shop-viewer (carol stays out).
        if group:
            idp.admin_add_user_to_group(UserPoolId=pool_id, Username=username, GroupName=group)
        users[username] = {"username": username, "password": password, "group": group}
    DEPLOY_DIR.mkdir(exist_ok=True)
    USERS_FILE.write_text(json.dumps(users, indent=2), encoding="utf-8")
    info(f"demo users alice/bob/carol ready, credentials in {USERS_FILE.relative_to(DEPLOY_DIR.parent)}")


def destroy(ctx: Context) -> None:
    """The domain must be deleted before the pool. Clients, groups and users go with the pool."""
    idp = ctx.client("cognito-idp")
    n = ctx.names
    pool_id = ctx.state.section("cognito").get("user_pool_id") or find_user_pool(idp, n.user_pool)
    if not pool_id:
        info("user pool already gone")
    else:
        # Step 1: delete the domain (only if it belongs to our pool) and wait until it is gone.
        domain = idp.describe_user_pool_domain(Domain=n.cognito_domain).get("DomainDescription", {})
        if domain.get("UserPoolId") == pool_id:
            idp.delete_user_pool_domain(Domain=n.cognito_domain, UserPoolId=pool_id)
            wait_for_status(
                f"domain {n.cognito_domain} deletion",
                lambda: (idp.describe_user_pool_domain(Domain=n.cognito_domain)["DomainDescription"].get("Status"), ""),
                ready={"DELETED"}, timeout=300,
            )
        # Step 2: delete the pool itself (AWS API DeleteUserPool); its clients, groups and users go with it.
        ignore_not_found(idp.delete_user_pool, UserPoolId=pool_id)
        info(f"{OK} deleted user pool {pool_id}")
        # Step 3: remove the local password file.
        # Only when this pool existed: with no pool, the file may belong to a Terraform deployment.
        if USERS_FILE.exists():
            USERS_FILE.unlink()
            info("removed .deploy/users.json")
    ctx.state.drop("cognito")
