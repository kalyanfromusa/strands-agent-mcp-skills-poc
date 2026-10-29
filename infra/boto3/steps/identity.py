"""AgentCore Identity: the OAuth2 credential provider `<prefix>-cognito`.

What this file does
    Deploy step 3. Registers the Cognito machine login (`<prefix>-m2m` client ID and secret) with
    AgentCore Identity, which keeps the secret safe and hands out login tokens on request.

Where it fits
    Needs the Cognito pool and M2M client from step 2. The IAM step (5) lets the Gateway role read this
    provider's secret, and the Gateway's `support` target (step 9) names this provider, so that at run
    time the Gateway can log in to the support MCP server.

A credential provider stores an OAuth2 client (id + secret) in the AgentCore token vault, backed by a
Secrets Manager secret the service creates and owns. AgentCore resources then ask the vault for tokens
instead of handling client secrets themselves. Here the Gateway uses it for **outbound** auth to the
support MCP runtime: before each call it fetches a client_credentials token (scope `<prefix>/invoke`) for
the Cognito M2M client.

Vendor `CustomOauth2` + the Cognito OIDC discovery URL: AgentCore reads the token endpoint from discovery.

Step by step
    1. Read the M2M client secret straight from Cognito (it is never written to state.json).
    2. Create the provider, or update it if it points at a different pool or client, or keep it.
    3. Wait until it is READY, then save its ARN and the ARN of its managed secret.

Key terms
    Credential provider: a stored login (client ID + secret) that AgentCore uses on your behalf.
    Token vault: AgentCore's secure store for such logins and the tokens they produce.
    Secrets Manager: the AWS service for storing passwords and keys; here the service manages the secret.
    Outbound auth: how the Gateway proves who it is when it calls a tool server.
"""

from __future__ import annotations

from common import Context, OK, ignore_not_found, info, is_not_found, wait_for_status


def build_provider_config(discovery_url: str, client_id: str, client_secret: str) -> dict:
    """The login details: where Cognito publishes its settings (discovery), and the client ID + secret."""
    return {
        "customOauth2ProviderConfig": {
            "oauthDiscovery": {"discoveryUrl": discovery_url},
            "clientId": client_id,
            "clientSecret": client_secret,
        }
    }


def build_create_provider_request(name: str, discovery_url: str, client_id: str, client_secret: str,
                                  tags: dict[str, str]) -> dict:
    """CreateOauth2CredentialProvider request. Vendor CustomOauth2 = any standard OAuth2 server (Cognito here)."""
    return {
        "name": name,
        "credentialProviderVendor": "CustomOauth2",
        "oauth2ProviderConfigInput": build_provider_config(discovery_url, client_id, client_secret),
        "tags": tags,
    }


def build_update_provider_request(name: str, discovery_url: str, client_id: str, client_secret: str) -> dict:
    """UpdateOauth2CredentialProvider request: the same settings, without tags."""
    return {
        "name": name,
        "credentialProviderVendor": "CustomOauth2",
        "oauth2ProviderConfigInput": build_provider_config(discovery_url, client_id, client_secret),
    }


def _get(ac, name: str) -> dict | None:
    """The provider's current details (AWS API GetOauth2CredentialProvider), or None if it doesn't exist."""
    try:
        return ac.get_oauth2_credential_provider(name=name)
    except Exception as err:  # noqa: BLE001
        if is_not_found(err):
            return None
        raise


def deploy(ctx: Context) -> None:
    """Create, update or keep the credential provider, and record its ARNs."""
    ac = ctx.client("bedrock-agentcore-control")
    idp = ctx.client("cognito-idp")
    n = ctx.names
    cog = ctx.state.section("cognito")
    pool_id, client_id, discovery_url = cog["user_pool_id"], cog["m2m_client_id"], cog["discovery_url"]
    # Step 1: AWS API DescribeUserPoolClient returns the client's settings, including its secret.
    client_secret = idp.describe_user_pool_client(UserPoolId=pool_id, ClientId=client_id)["UserPoolClient"][
        "ClientSecret"]

    # Step 2: create it if missing; otherwise compare client ID and discovery URL with what we want.
    current = _get(ac, n.oauth_provider)
    if current is None:
        resp = ac.create_oauth2_credential_provider(
            **build_create_provider_request(n.oauth_provider, discovery_url, client_id, client_secret, n.tags))
        info(f"{OK} created OAuth2 credential provider {n.oauth_provider}")
    else:
        # The secret itself can't be read back, so only the client ID and discovery URL are compared.
        cfg = current.get("oauth2ProviderConfigOutput", {}).get("customOauth2ProviderConfig", {})
        same = (cfg.get("clientId") == client_id
                and cfg.get("oauthDiscovery", {}).get("discoveryUrl") == discovery_url)
        if same:
            resp = current
            info(f"credential provider {n.oauth_provider} exists and matches")
        else:
            # Typical after the user pool was recreated: point the provider at the new pool/client.
            resp = ac.update_oauth2_credential_provider(
                **build_update_provider_request(n.oauth_provider, discovery_url, client_id, client_secret))
            info(f"{OK} updated credential provider {n.oauth_provider} (new client or pool)")

    def status() -> tuple[str | None, str]:
        """(status, failure reason) of the provider, for the wait below."""
        p = _get(ac, n.oauth_provider) or {}
        return p.get("status"), p.get("failureReason", "")

    # Step 3: wait for READY (up to 3 minutes), then save the provider ARN and its secret's ARN.
    # The IAM step uses the secret ARN to let the Gateway read exactly this one secret.
    wait_for_status(f"credential provider {n.oauth_provider}", status,
                    ready={"READY"}, failed={"CREATE_FAILED", "UPDATE_FAILED", "DELETED"}, timeout=180)
    ctx.state.put(
        "identity",
        name=n.oauth_provider,
        arn=resp["credentialProviderArn"],
        secret_arn=resp["clientSecretArn"]["secretArn"],  # the service-managed Secrets Manager secret
    )


def destroy(ctx: Context) -> None:
    """Delete the credential provider (AWS API DeleteOauth2CredentialProvider)."""
    ac = ctx.client("bedrock-agentcore-control")
    name = ctx.names.oauth_provider
    if ignore_not_found(ac.delete_oauth2_credential_provider, name=name) is None:
        info(f"credential provider {name} already gone")
    else:
        info(f"{OK} deleted credential provider {name} (its managed secret is deleted with it)")
    ctx.state.drop("identity")
