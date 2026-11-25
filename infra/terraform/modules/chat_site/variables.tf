# =====================================================================================================
# modules/chat_site/variables.tf: inputs of the hosted chat page module
#
# What this file does
#   Declares the values the chat_site module needs: naming, where the page files live in the
#   repository, the Cognito address the page may call, and the contents of config.json.
#
# Where it fits
#   Filled in by the module "chat_site" block in infra/terraform/main.tf. The config map is built
#   there from the identity and chat_relay modules.
#
# Step by step
#   1. Receive the naming values (prefix with environment, account, region).
#   2. Receive the folder with chat_ui.html and chat_auth.js.
#   3. Receive the Cognito sign-in address (for the page's Content Security Policy).
#   4. Receive the settings the page reads from config.json.
#
# Key terms
#   CSP (Content Security Policy): a response header that tells the browser which addresses the page
#     may load scripts from and send requests to.
# =====================================================================================================

# Naming: name_prefix includes the environment (e.g. shopassist-prod). account_id and region make the
# bucket name globally unique; region also builds the relay's address pattern in the CSP.
variable "name_prefix" { type = string }
variable "account_id" { type = string }
variable "region" { type = string }

# The repository's scripts/ folder: chat_ui.html is published as index.html, chat_auth.js as is.
variable "page_dir" { type = string }

# Cognito's sign-in address (https://<domain>.auth.<region>.amazoncognito.com). The page sends the
# token request there, so the CSP must allow it.
variable "cognito_domain_url" { type = string }

# The public settings the page loads at start-up (design.md section 12): environment, region,
# cognito_domain_url, client_id, relay_url, entra_enabled. No secrets.
variable "config" {
  type = map(string)
}
