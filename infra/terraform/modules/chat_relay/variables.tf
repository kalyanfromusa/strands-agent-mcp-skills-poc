# =====================================================================================================
# modules/chat_relay/variables.tf: inputs of the chat relay Lambda module
#
# What this file does
#   Declares the values the relay module needs: naming, its container image, the Runtime address it
#   forwards to, the page origin it serves, and its concurrency cap.
#
# Where it fits
#   Filled in by the module "chat_relay" block in infra/terraform/main.tf.
#
# Step by step
#   1. Receive the image URI and the permissions boundary from the root main.tf.
#   2. Receive the agent Runtime URL and the page origin (the CloudFront address).
#   3. Receive the reserved concurrency and the log retention.
#
# Key terms
#   Reserved concurrency: the number of copies of the function that may run at once; it is both a
#     guarantee and a hard cap, so it limits spend if someone floods the public address.
# =====================================================================================================

# Naming (name_prefix includes the environment), the container image (<repo>:<git sha>) and the
# permissions boundary every role must carry.
variable "name_prefix" { type = string }
variable "image_uri" { type = string }
variable "permissions_boundary_arn" { type = string }

# Where the relay forwards the user's request: the agent Runtime /invocations URL.
variable "runtime_url" { type = string }

# The page origin, https://<cloudfront domain> (no trailing slash). Used for CORS and the Origin check.
variable "allowed_origin" { type = string }

# At most this many requests run at once; the rest get HTTP 429. null = no reservation, for accounts
# whose Lambda concurrency limit is too low to reserve (the apply fails with an "unreserved
# concurrency" error otherwise).
variable "reserved_concurrency" {
  type     = number
  default  = 5
  nullable = true
}

# How many days CloudWatch keeps the function's logs.
variable "log_retention_days" { type = number }
