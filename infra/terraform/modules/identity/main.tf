# =====================================================================================================
# modules/identity/main.tf: who may log in, and how services prove who they are
#
# What this file does
#   Creates the Cognito user pool (the login service) with its demo users alice, bob and carol, two
#   app clients (one for people, one for machines), and an AgentCore Identity credential provider that
#   lets the Gateway fetch its own machine token. Optionally (enable_entra) it also adds Microsoft
#   Entra ID as a second way to sign in and hooks the role-mapper Lambda into token issuing.
#
# Where it fits
#   Called by the root main.tf (module "identity"). Its outputs are used by:
#     - agent_runtime and gateway: to check incoming login tokens (discovery URL, allowed client ids);
#     - mcp_runtime: to accept only machine tokens from the M2M client;
#     - gateway: the credential provider, used when the Gateway calls the support server;
#     - root main.tf: the demo users' passwords, written to .deploy/<env>/users.json, and the ids and
#       addresses written to .deploy/<env>/outputs.json.
#
# Step by step
#   1. Create the user pool <prefix>-pool, where only an administrator (Terraform) can create users.
#      With Entra on, attach the role mapper as the pre-token-generation trigger.
#   2. Give it a domain, which hosts the sign-in pages and the /oauth2/token address.
#   3. Define the custom permission (scope) shopassist/invoke.
#   4. Create the M2M app client (has a secret, used by the Gateway and local tests).
#   5. Create the users app client (no secret, used by people signing in from the CLI or the local
#      chat page, with a password or through the browser).
#      5a. Only with enable_entra: register Entra ID as an OIDC identity provider ("EntraID").
#      5b. Only with enable_entra: allow this pool to call the role-mapper Lambda.
#      5c. Create the web app client (no secret, used by the hosted chat page on CloudFront with the
#      browser code + PKCE flow).
#   6. Create the groups Group-ShopAssist-Admin and Group-ShopAssist-Viewer.
#   7. Generate a random password per demo user, create alice, bob and carol, and put alice in
#      Group-ShopAssist-Admin and bob in Group-ShopAssist-Viewer (carol gets no group).
#   8. Register the M2M client with AgentCore Identity as an OAuth2 credential provider.
#
# Who flows through which client (Cognito is the single OAuth2 authorization server for the PoC)
#   - users client : humans (alice/bob/carol, and Entra users when enabled) -> agent runtime ->
#                    gateway (the same user token is passed along, "token passthrough")
#   - m2m client   : gateway -> support MCP runtime (the Gateway logs in as itself) and local dev
#
# Key terms
#   Cognito user pool: an AWS-managed list of users and passwords that issues login tokens.
#   App client: an application registered with the pool; each token records which client it came from.
#   OAuth2: the standard rules for issuing and checking access tokens.
#   M2M (machine-to-machine): a service logging in as itself with a client id and secret, no person.
#   Scope: a named permission written into a token, here "shopassist/invoke".
#   Discovery URL: a public web address that tells other services how to check this pool's tokens.
#   Credential provider: an AgentCore Identity entry that stores a client secret and fetches tokens.
#   OIDC (OpenID Connect) identity provider: an outside login service (here Entra ID) that Cognito
#     trusts to say who a user is ("federation").
#   PKCE (Proof Key for Code Exchange): a safety step for browser sign-in by apps that can't keep a
#     secret, so a stolen sign-in code is useless.
# =====================================================================================================

# Values built from the pool id and region, shared by several resources and outputs.
#   domain:        the Cognito domain prefix, e.g. shopassist-prod-<account>. It stays the same across
#                  destroy and redeploy, so the Entra redirect address only has to be registered once.
#   discovery_url: where the Runtime and Gateway authorizers learn how to verify tokens.
#   token_url:     where the M2M client (and the scripts) exchange the id and secret for a token.
#   domain_url:    the base address of Cognito's sign-in pages and token endpoint for this pool.
#   groups:        the two Cognito groups the access rules use.
locals {
  domain        = "${var.name_prefix}-${var.account_id}"
  discovery_url = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.this.id}/.well-known/openid-configuration"
  domain_url    = "https://${local.domain}.auth.${var.region}.amazoncognito.com"
  token_url     = "${local.domain_url}/oauth2/token"
  groups        = toset(["Group-ShopAssist-Admin", "Group-ShopAssist-Viewer"])
}

# Step 1: the user pool. Self sign-up is off and the password rules are strict (12+ characters with
# lower case, upper case, numbers and symbols); the random passwords below meet them.
resource "aws_cognito_user_pool" "this" {
  name = "${var.name_prefix}-pool"

  # Only an administrator (Terraform) can create Cognito users; Entra users are created by Cognito
  # itself at their first federated sign-in.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length    = 12
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = true
  }

  # Where Cognito keeps what Entra says about a federated user (refreshed at every sign-in):
  #   custom:entra_roles   the app roles (AppRole-ShopAssist-*), used in "roles" mode;
  #   custom:entra_groups  the group object ids, used in the Entra-Free "groups" mode.
  # Always defined (also with enable_entra = false), so switching Entra on later is an in-place change.
  schema {
    name                     = "entra_roles"
    attribute_data_type      = "String"
    mutable                  = true
    developer_only_attribute = false
    string_attribute_constraints {
      min_length = 0
      max_length = 2048
    }
  }
  schema {
    name                     = "entra_groups"
    attribute_data_type      = "String"
    mutable                  = true
    developer_only_attribute = false
    string_attribute_constraints {
      min_length = 0
      max_length = 2048
    }
  }

  # The role mapper runs just before Cognito issues tokens (only when Entra is on). A `dynamic` block
  # with an empty list writes no block at all, so with Entra off the pool has no trigger.
  dynamic "lambda_config" {
    for_each = var.enable_entra ? [1] : []
    content {
      pre_token_generation_config {
        lambda_arn     = var.role_mapper_arn
        lambda_version = var.trigger_version
      }
    }
  }
}

# Step 2: the pool's Cognito-hosted domain https://<domain>.auth.<region>.amazoncognito.com. It
# serves the managed sign-in page (browser sign-in, "Sign in with Entra") and /oauth2/token, where
# the M2M client and the scripts get their tokens.
resource "aws_cognito_user_pool_domain" "this" {
  domain       = local.domain
  user_pool_id = aws_cognito_user_pool.this.id
}

# Step 3: a "resource server" is Cognito's name for an API that tokens can grant access to.
# It defines the scope shopassist/invoke (the same in every environment, each env has its own pool),
# which machine tokens must carry to call the support tools.
resource "aws_cognito_resource_server" "api" {
  user_pool_id = aws_cognito_user_pool.this.id
  identifier   = var.api_identifier
  name         = "${var.name_prefix}-api"

  scope {
    scope_name        = "invoke"
    scope_description = "Call ShopAssist MCP tools"
  }
}

# Step 4: the M2M client. The Gateway (through the credential provider) and local test scripts use
# its id and secret to get a token without any person logging in.
# Machine-to-machine client: client_credentials grant needs a secret and a custom scope.
resource "aws_cognito_user_pool_client" "m2m" {
  name         = "${var.name_prefix}-m2m"
  user_pool_id = aws_cognito_user_pool.this.id

  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["client_credentials"]
  allowed_oauth_scopes                 = aws_cognito_resource_server.api.scope_identifiers
  supported_identity_providers         = ["COGNITO"]
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH"]
}

# Step 5: the users client. scripts/invoke_agent.py logs in as alice, bob or carol with a username
# and password through this client and sends the resulting token to the agent; the browser sign-in
# (chat page or `invoke_agent.py --login`) goes through it as well.
# Its access token carries cognito:groups, which the runtime authorizer and Cedar check.
# depends_on: the "EntraID" provider must exist before the client may list it.
resource "aws_cognito_user_pool_client" "users" {
  name         = "${var.name_prefix}-users"
  user_pool_id = aws_cognito_user_pool.this.id

  # Public client (no secret): it's used from a browser and the CLI on the user's own machine.
  generate_secret = false

  # 1. Password sign-in for the Cognito demo users (scripts/invoke_agent.py --user alice).
  explicit_auth_flows = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]

  # 2. Browser sign-in through Cognito's managed login (authorization code + PKCE), offering the Cognito
  #    users and, when enabled, "Sign in with Entra". The redirect goes back to the user's own machine
  #    (loopback): the chat page (8790) or the CLI (8791).
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  callback_urls                        = var.callback_urls
  logout_urls                          = var.logout_urls
  supported_identity_providers         = var.enable_entra ? ["COGNITO", "EntraID"] : ["COGNITO"]

  # Short lifetimes bound how long a removed right survives. Access and ID tokens last 15 minutes,
  # refresh tokens 8 hours. A refresh re-runs the role mapper, but on the attributes Cognito stored at
  # the user's last Entra sign-in (Cognito only re-reads Entra at sign-in). So removing someone from
  # an AD group takes full effect at their next Entra sign-in, at the latest when the 8-hour refresh
  # token runs out. For an immediate cut-off, sign the user out globally (documented runbook step).
  access_token_validity  = 15
  id_token_validity      = 15
  refresh_token_validity = 8
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "hours"
  }

  # Don't reveal whether a user name exists.
  prevent_user_existence_errors = "ENABLED"

  depends_on = [aws_cognito_identity_provider.entra]
}

# Step 5c: the web client, used only by the hosted chat page (module chat_site). Like the users client
# it is public (no secret) and uses the authorization code flow with PKCE, but it has no password flow
# and its only callback and logout address is the CloudFront page (https://<domain>/, trailing slash).
# It offers the same sign-in options and token lifetimes as the users client. Its id must be in the
# Runtime's and the Gateway's allowed_client_ids, or every hosted call is a 401.
# depends_on: the "EntraID" provider must exist before the client may list it.
resource "aws_cognito_user_pool_client" "web" {
  name         = "${var.name_prefix}-web"
  user_pool_id = aws_cognito_user_pool.this.id

  generate_secret     = false
  explicit_auth_flows = ["ALLOW_REFRESH_TOKEN_AUTH"]

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  callback_urls                        = var.web_callback_urls
  logout_urls                          = var.web_callback_urls
  supported_identity_providers         = var.enable_entra ? ["COGNITO", "EntraID"] : ["COGNITO"]

  access_token_validity  = 15
  id_token_validity      = 15
  refresh_token_validity = 8
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "hours"
  }

  prevent_user_existence_errors = "ENABLED"

  depends_on = [aws_cognito_identity_provider.entra]
}
# ---------------------------------------------------------------- Entra ID federation (enable_entra)

# Step 5a (only with enable_entra): Entra ID as an OpenID Connect identity provider. Users choose
# "EntraID" on the sign-in page, sign in at Microsoft, and come back with an ID token whose claims
# Cognito copies into the user's attributes (attribute_mapping, Cognito name = Entra claim):
#   roles  -> custom:entra_roles  (app roles assigned through AD groups, "roles" mode)
#   groups -> custom:entra_groups (group object ids, "groups" mode), plus email and name
#   username = Entra's "sub" claim, a stable id that does not change when the user is renamed.
# The client secret comes from the GitHub environment secret, never from a file.
resource "aws_cognito_identity_provider" "entra" {
  count = var.enable_entra ? 1 : 0

  user_pool_id  = aws_cognito_user_pool.this.id
  provider_name = "EntraID"
  provider_type = "OIDC"

  provider_details = {
    client_id                 = var.entra_client_id
    client_secret             = var.entra_client_secret
    oidc_issuer               = "https://login.microsoftonline.com/${var.entra_tenant_id}/v2.0"
    authorize_scopes          = "openid email profile"
    attributes_request_method = "GET"
  }

  attribute_mapping = {
    username              = "sub"
    email                 = "email"
    name                  = "name"
    "custom:entra_roles"  = "roles"
    "custom:entra_groups" = "groups"
  }
}

# Step 5b (only with enable_entra): let Cognito invoke the role mapper, and only on behalf of this
# user pool (source_arn), so no other pool can use the function.
resource "aws_lambda_permission" "role_mapper" {
  count = var.enable_entra ? 1 : 0

  statement_id  = "AllowCognitoPreTokenGeneration"
  action        = "lambda:InvokeFunction"
  function_name = var.role_mapper_arn
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.this.arn
}

# ---------------------------------------------------------------- Demo users & groups

# Step 6: the two groups. Group-ShopAssist-Admin may use every tool; Group-ShopAssist-Viewer only the read-only ones
# (that rule lives in infra/policies/*.cedar). Group names appear in the user's token.
resource "aws_cognito_user_group" "this" {
  for_each     = local.groups
  name         = each.value
  user_pool_id = aws_cognito_user_pool.this.id
}

# Step 7a: a random 20-character password per demo user, with at least two of each character type
# so it always meets the pool's password rules. Terraform keeps it in state, so it stays the same on
# later runs instead of changing every time.
resource "random_password" "user" {
  for_each = var.demo_users

  length      = 20
  min_lower   = 2
  min_upper   = 2
  min_numeric = 2
  min_special = 2
  # Characters Cognito accepts as symbols and that need no quoting in shells/JSON.
  override_special = "!#%*-_=+.:"
}

# Step 7b: create alice, bob and carol in the pool with those passwords.
resource "aws_cognito_user" "this" {
  for_each = var.demo_users

  user_pool_id = aws_cognito_user_pool.this.id
  username     = each.key
  # Setting `password` (not temporary_password) makes it permanent: no
  # FORCE_CHANGE_PASSWORD challenge in the demo. SUPPRESS: there is no real mailbox.
  password       = random_password.user[each.key].result
  message_action = "SUPPRESS"
}

# Step 7c: put each user in their group (alice -> Group-ShopAssist-Admin, bob -> Group-ShopAssist-Viewer). Users whose group
# is "" (carol) are skipped, which is how carol ends up with no access at all.
resource "aws_cognito_user_in_group" "this" {
  for_each = { for u, g in var.demo_users : u => g if g != "" }

  user_pool_id = aws_cognito_user_pool.this.id
  username     = aws_cognito_user.this[each.key].username
  group_name   = aws_cognito_user_group.this[each.value].name
}

# ---------------------------------------------------------------- AgentCore Identity

# Step 8: register the M2M client (id + secret) with AgentCore Identity. "CustomOauth2" means "any
# standard OAuth2 server", here our Cognito pool, found through its discovery URL. depends_on makes
# Terraform wait for the domain and the scope, because the provider reads the discovery document,
# which only lists the token address once the domain exists.
# The gateway uses this provider to mint an M2M token for the `support` target.
# The client secret is copied into an AgentCore-managed Secrets Manager secret
# (exported as client_secret_arn) and also lands in Terraform state (PoC trade-off).
resource "aws_bedrockagentcore_oauth2_credential_provider" "cognito" {
  name                       = "${var.name_prefix}-cognito"
  credential_provider_vendor = "CustomOauth2"

  oauth2_provider_config {
    custom_oauth2_provider_config {
      client_id     = aws_cognito_user_pool_client.m2m.id
      client_secret = aws_cognito_user_pool_client.m2m.client_secret

      oauth_discovery {
        discovery_url = local.discovery_url
      }
    }
  }

  depends_on = [aws_cognito_user_pool_domain.this, aws_cognito_resource_server.api]
}
