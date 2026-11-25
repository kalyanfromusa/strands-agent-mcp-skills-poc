# =====================================================================================================
# modules/chat_relay/outputs.tf: what the chat_relay module hands to the root stack
#
# What this file does
#   Publishes the relay's function URL (the address the chat page posts to).
#
# Where it fits
#   The root main.tf writes it into the page's config.json (relay_url) and into
#   .deploy/<env>/outputs.json as chat_relay_url.
#
# Step by step
#   1. Report the function URL, with a trailing slash as AWS returns it.
#
# Key terms
#   Function URL: a built-in HTTPS address of a Lambda function, with no API Gateway in front.
# =====================================================================================================

output "function_url" { value = aws_lambda_function_url.this.function_url }
