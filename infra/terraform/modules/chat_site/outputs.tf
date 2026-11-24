# =====================================================================================================
# modules/chat_site/outputs.tf: what the chat_site module hands to the other modules
#
# What this file does
#   Publishes the CloudFront address of the chat page, the distribution id and the bucket name.
#
# Where it fits
#   The root main.tf passes domain_name to module "identity" (Cognito callback URL) and module
#   "chat_relay" (CORS origin), and writes url, distribution_id and bucket to .deploy/<env>/outputs.json.
#   deploy.yml reads the distribution id to clear the CloudFront cache after an apply.
#
# Step by step
#   1. Report the CloudFront domain name and the page URL (https://<domain>/).
#   2. Report the distribution id and the bucket name.
#
# Key terms
#   Distribution: CloudFront's name for one website configuration, with its own *.cloudfront.net name.
# =====================================================================================================

# The distribution's own address, e.g. d111111abcdef8.cloudfront.net (no scheme, no slash).
output "domain_name" { value = aws_cloudfront_distribution.this.domain_name }

# The page address. The trailing slash is part of the Cognito callback and logout URL.
output "url" { value = "https://${aws_cloudfront_distribution.this.domain_name}/" }

# Used by the deploy job to invalidate the cache.
output "distribution_id" { value = aws_cloudfront_distribution.this.id }

output "bucket" { value = aws_s3_bucket.this.id }
