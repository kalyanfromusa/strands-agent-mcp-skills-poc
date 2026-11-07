# =====================================================================================================
# modules/identity/main.tf: who may log in, and how services prove who they are
#
# What this file does
#   Creates the Cognito user pool (the login service) with its demo users alice, bob and carol, two
#   app clients (one for people, one for machines), and an AgentCore Identity credential provider that
#   lets the Gateway fetch its own machine token.
#
# Where it fits
#   Called by the root main.tf (module "identity"). Its outputs are used by:
#     - agent_runtime and gateway: to check incoming login tokens (discovery URL, allowed client ids);
#     - mcp_runtime: to accept only machine tokens from the M2M client;
#     - gateway: the credential provider, used when the Gateway calls the support server;
#     - root main.tf: the demo users' passwords, written to .deploy/users.json.
#
# Step by step
#   1. Create the user pool <prefix>-pool, where only an administrator (Terraform) can create users.
#   2. Give it a domain, which hosts the /oauth2/token address that hands out tokens.
#   3. Define the custom permission (scope) shopassist/invoke.
#   4. Create the M2M app client (has a secret, used by the Gateway and local tests).
#   5. Create the users app client (no secret, used by people logging in from the CLI).
#   6. Create the groups Group-ShopAssist-Admin and Group-ShopAssist-Viewer.
#   7. Generate a random password per demo user, create alice, bob and carol, and put alice in
#      Group-ShopAssist-Admin and bob in Group-ShopAssist-Viewer (carol gets no group).
#   8. Register the M2M client with AgentCore Identity as an OAuth2 credential provider.
#
# Who flows through which client (Cognito is the single OAuth2 authorization server for the PoC)
#   - users client : humans (alice/bob/carol) -> agent runtime -> gateway (the same user token is
#                    passed along, "token passthrough")
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
# =====================================================================================================

# Values built from the pool id and region, shared by several resources and outputs.
#   domain:        the Cognito domain prefix, unique per account.
#   discovery_url: where the Runtime and Gateway authorizers learn how to verify tokens.
#   token_url:     where the M2M client (and the scripts) exchange the id and secret for a token.
#   groups:        the two Cognito groups the access rules use.
locals {
  domain        = "${var.name_prefix}-${var.account_id}"
  discovery_url = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.this.id}/.well-known/openid-configuration"
  token_url     = "https://${local.domain}.auth.${var.region}.amazoncognito.com/oauth2/token"
  groups        = toset(["Group-ShopAssist-Admin", "Group-ShopAssist-Viewer"])
}

# Step 1: the user pool. Self sign-up is off and the password rules are strict (12+ characters with
# lower case, upper case, numbers and symbols); the random passwords below meet them.
resource "aws_cognito_user_pool" "this" {
  name = "${var.name_prefix}-pool"

  # Demo users are created by Terraform with a permanent password; nobody signs up.
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
}

# Step 2: the pool's web domain, <prefix>-<account>.auth.<region>.amazoncognito.com.
# The domain hosts /oauth2/token. It must exist before anything reads the discovery
# document, because Cognito only advertises token_endpoint once a domain is set.
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
# and password through this client and sends the resulting token to the agent.
# Public client for humans: no secret, so a CLI can do USER_PASSWORD_AUTH directly.
# Its access token carries cognito:groups, which the runtime authorizer and Cedar check.
resource "aws_cognito_user_pool_client" "users" {
  name         = "${var.name_prefix}-users"
  user_pool_id = aws_cognito_user_pool.this.id

  generate_secret     = false
  explicit_auth_flows = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
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
