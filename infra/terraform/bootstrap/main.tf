# =====================================================================================================
# bootstrap/main.tf: the S3 bucket that stores Terraform's state for every other ShopAssist stack
#
# What this file does
#   Creates one private, encrypted, versioned S3 bucket, shopassist-tfstate-<account>-<region>. The
#   `platform` stack and the main stack (each environment) keep their state files in it.
#
# Where it fits
#   It is the first thing you apply, once per AWS account, and the last thing you destroy. It has to
#   exist before any other stack can use the S3 backend, so this stack keeps its own state in a local
#   file (infra/terraform/bootstrap/terraform.tfstate, git-ignored). That file is tiny and easy to
#   recreate with `terraform import` if it is ever lost.
#
#   Order: bootstrap -> platform -> main (envs/prod). Destroy in the reverse order.
#
# Step by step
#   1. Look up the account id (part of the bucket name, which must be globally unique).
#   2. Create the bucket. force_destroy lets `terraform destroy` empty it first (cost hygiene).
#   3. Versioning is OFF (decided 2026-11-06: no S3 versioning). The bucket was first created with it
#      on, and S3 can't return a bucket to "never versioned", so it is set to Suspended: new writes keep
#      no old copies. Trade-off: a corrupted state file can't be restored from an older version.
#   4. Encrypt every object at rest (SSE-S3) and block every form of public access.
#   5. A bucket policy that refuses any request not sent over HTTPS (TLS).
#   6. A lifecycle rule that cleans up unfinished uploads and any old copies left from before step 3.
#
# Key terms
#   State: Terraform's record of what it created (ids, ARNs, generated passwords). It is sensitive,
#     which is why the bucket is private, encrypted and TLS-only.
#   Backend: where Terraform keeps state. "S3 backend" = in this bucket, with a lock file (.tflock)
#     next to each state file, so two runs can't change the same stack at once (use_lockfile = true).
#   Versioning: S3 keeping every previous copy of a file instead of overwriting it (switched off here).
# =====================================================================================================

# Step 1: which account are we in?
data "aws_caller_identity" "current" {}

locals {
  bucket_name = "${var.name_prefix}-tfstate-${data.aws_caller_identity.current.account_id}-${var.region}"
}

# Step 2: the bucket itself.
resource "aws_s3_bucket" "state" {
  # Accepted checkov findings for a low-cost PoC (reviewed 2026-11-06):
  #checkov:skip=CKV_AWS_18:Access logging needs a second bucket; CloudTrail already records who touched state.
  #checkov:skip=CKV_AWS_144:Cross-region replication doubles cost and needs versioning, which is off by decision.
  #checkov:skip=CKV_AWS_21:Versioning is off by decision (2026-11-06).
  #checkov:skip=CKV_AWS_145:SSE-S3 (AES256) is used; a KMS key would add about 1 USD/month.
  #checkov:skip=CKV2_AWS_62:No consumer for S3 event notifications.
  bucket        = local.bucket_name
  force_destroy = true # cost hygiene: `terraform destroy` removes every state version too
}

# Step 3: no versioning (Suspended, because S3 can't go back to "never enabled").
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Suspended"
  }
}

# Step 4a: encrypt everything at rest with S3-managed keys (free).
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# Step 4b: no public access, ever (blocks public ACLs and public bucket policies).
resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Step 4c: the bucket owner owns every object; ACLs (old-style per-object permissions) are switched off.
resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Step 5: refuse plain-HTTP requests. "aws:SecureTransport = false" means "not sent over TLS".
data "aws_iam_policy_document" "tls_only" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    resources = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = data.aws_iam_policy_document.tls_only.json

  # The public access block must exist first, or S3 may reject the policy call.
  depends_on = [aws_s3_bucket_public_access_block.state]
}

# Step 6: housekeeping. Unfinished uploads go after 1 day; old copies from before versioning was suspended go after 1 day.
resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 1
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  depends_on = [aws_s3_bucket_versioning.state]
}
