# =====================================================================================================
# modules/chat_site/main.tf: the hosted chat page (private S3 bucket behind CloudFront)
#
# What this file does
#   Publishes the chat page on the internet: a private S3 bucket holding index.html, chat_auth.js and
#   config.json, and a CloudFront distribution in front of it that serves them over HTTPS with
#   security headers. Users sign in on the page itself with the Cognito client <prefix>-web.
#
# Where it fits
#   Called by the root main.tf (module "chat_site"). Its domain name goes to module "identity" (the
#   Cognito callback URL) and module "chat_relay" (CORS). The page files are the same ones the local
#   chat server uses (scripts/chat_ui.html, scripts/chat_auth.js); config.json is generated here from
#   values of the other modules, so the page knows its Cognito client and its relay address.
#
# Step by step
#   1. Create the private bucket <prefix>-chat-ui-<account>-<region> (all public access blocked,
#      force_destroy so `terraform destroy` empties it).
#   2. Create the origin access control (OAC) that lets CloudFront, and only CloudFront, read it.
#   3. Create the response-headers policy: HSTS, nosniff, no referrer, and the Content Security Policy.
#   4. Create the CloudFront distribution (HTTPS only, cheapest price class, default *.cloudfront.net
#      name, no custom domain).
#   5. Allow that one distribution to read the bucket (bucket policy).
#   6. Upload index.html, chat_auth.js and config.json (Cache-Control: no-cache). deploy.yml then
#      invalidates the CloudFront cache so a changed page shows at once.
#
# Key terms
#   CloudFront: AWS's content delivery network; it serves the files from edge locations near the user.
#   OAC (origin access control): the way CloudFront signs its requests to a private S3 bucket.
#   HSTS: a header telling browsers to use HTTPS only for this site.
#   Cache-Control: no-cache: the edge may keep a copy but must check S3 (by ETag) before reusing it.
#   Invalidation: telling CloudFront to forget its cached copies.
#   Public client: a Cognito app client without a secret; safe in a browser because of PKCE.
# =====================================================================================================

# The managed cache policy "CachingOptimized" (an AWS-provided id, the same in every account).
# The names follow the pattern shopassist-<env>-chat-ui so scripts/leftovers.py finds them.
locals {
  name              = "${var.name_prefix}-chat-ui"
  bucket_name       = "${var.name_prefix}-chat-ui-${var.account_id}-${var.region}"
  cache_policy_id   = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  origin_id         = "s3-chat-ui"
  relay_origin_glob = "https://*.lambda-url.${var.region}.on.aws"

  # The page may load scripts and styles only from itself (its inline script is part of the page),
  # and may send requests only to itself (config.json), the relay's function URL (the relay's exact
  # address is not known here without a dependency loop, so the region's function-URL pattern is
  # used) and the Cognito sign-in address (token exchange).
  csp = join("; ", [
    "default-src 'none'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'unsafe-inline'",
    "connect-src 'self' ${local.relay_origin_glob} ${var.cognito_domain_url}",
    "img-src 'self' data:",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
  ])
}

# Step 1: the private bucket. Nothing in it is public; only CloudFront may read it (step 5).
# force_destroy = true: cost hygiene, `terraform destroy` must not stop at a non-empty bucket.
# No versioning on purpose (decisions.md 2026-11-07): the page is rebuilt from the repository.
resource "aws_s3_bucket" "this" {
  bucket        = local.bucket_name
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket                  = aws_s3_bucket.this.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Step 2: the origin access control. CloudFront signs every request to the bucket with SigV4.
resource "aws_cloudfront_origin_access_control" "this" {
  name                              = local.name
  description                       = "Chat UI bucket access for ${var.name_prefix}"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Step 3: security headers added to every response. The CSP is the main protection for a page that
# holds an access token in memory: no outside scripts, no framing, requests only to known addresses.
resource "aws_cloudfront_response_headers_policy" "this" {
  name    = local.name
  comment = "Security headers for the ${var.name_prefix} chat page"

  security_headers_config {
    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      override                   = true
    }
    content_type_options {
      override = true
    }
    referrer_policy {
      referrer_policy = "no-referrer"
      override        = true
    }
    content_security_policy {
      content_security_policy = local.csp
      override                = true
    }
  }
}

# Step 4: the distribution. CachingOptimized plus no-cache objects means "keep a copy, but revalidate
# with S3 each time". PriceClass_100 serves from the cheapest edge locations (North America, Europe).
# The default *.cloudfront.net certificate is used, so there is no custom domain and no certificate.
# The comment equals the resource name so scripts/leftovers.py can find the distribution.
resource "aws_cloudfront_distribution" "this" {
  enabled             = true
  comment             = local.name
  default_root_object = "index.html"
  price_class         = "PriceClass_100"
  http_version        = "http2"

  origin {
    domain_name              = aws_s3_bucket.this.bucket_regional_domain_name
    origin_id                = local.origin_id
    origin_access_control_id = aws_cloudfront_origin_access_control.this.id
  }

  default_cache_behavior {
    target_origin_id           = local.origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = local.cache_policy_id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.this.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

# Step 5: only this distribution (matched by its ARN) may read objects from the bucket.
data "aws_iam_policy_document" "bucket" {
  statement {
    sid       = "AllowCloudFrontRead"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.this.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.this.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "this" {
  bucket = aws_s3_bucket.this.id
  policy = data.aws_iam_policy_document.bucket.json

  depends_on = [aws_s3_bucket_public_access_block.this]
}

# Step 6: the three files. Terraform uploads them (like the skills in module "data"), so the plan
# shows a page change and `terraform destroy` removes them. The etag makes an edit to a file an
# in-place update. config.json has no secrets: ids and addresses only.
resource "aws_s3_object" "index" {
  bucket        = aws_s3_bucket.this.id
  key           = "index.html"
  source        = "${var.page_dir}/chat_ui.html"
  etag          = filemd5("${var.page_dir}/chat_ui.html")
  content_type  = "text/html; charset=utf-8"
  cache_control = "no-cache"
}

resource "aws_s3_object" "auth_js" {
  bucket        = aws_s3_bucket.this.id
  key           = "chat_auth.js"
  source        = "${var.page_dir}/chat_auth.js"
  etag          = filemd5("${var.page_dir}/chat_auth.js")
  content_type  = "application/javascript"
  cache_control = "no-cache"
}

resource "aws_s3_object" "config" {
  bucket        = aws_s3_bucket.this.id
  key           = "config.json"
  content       = jsonencode(var.config)
  etag          = md5(jsonencode(var.config))
  content_type  = "application/json"
  cache_control = "no-cache"
}
