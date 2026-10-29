# =====================================================================================================
# ci/ecr.tf: the two container image repositories
#
# What this file does
#   Creates one private ECR repository per containerized runtime, <prefix>-agent and
#   <prefix>-support-mcp, with vulnerability scanning on push and a cleanup rule that keeps only the
#   last few images.
#
# Where it fits
#   The GitHub workflow (job build-and-push) pushes <repo>:<git sha> and <repo>:latest here. In
#   container mode the AgentCore runtimes pull their image from here (their execution roles need ECR
#   pull permissions, added in the main stack: docs/ci-cd-integration-patch.md).
#
# Step by step
#   1. Create both repositories from one map (for_each), mutable tags (`latest` moves on every push),
#      scan on push, AES-256 encryption, force_delete so `terraform destroy` works with images inside.
#   2. Attach a lifecycle policy: keep the newest N images, expire older ones automatically.
#
# Key terms
#   ECR (Elastic Container Registry): AWS's private Docker image store; billed per GB stored.
#   Image tag: a name for one image version, e.g. the git commit SHA or `latest`.
#   Mutable tags: a tag can be moved to a newer image (needed for `latest`). The SHA tags are unique
#     anyway, so rollbacks stay exact.
#   Scan on push: ECR's basic scan lists known vulnerabilities (CVEs) of the OS packages in an image.
# =====================================================================================================

# repos: logical name -> repository name. The keys are reused in outputs.tf.
locals {
  repos = {
    agent       = "${var.name_prefix}-agent"
    support_mcp = "${var.name_prefix}-support-mcp"
  }
}

# Step 1: the repositories.
resource "aws_ecr_repository" "this" {
  for_each = local.repos

  name                 = each.value
  image_tag_mutability = "MUTABLE" # `latest` is re-pointed on every push; SHA tags never repeat
  force_delete         = true      # PoC: destroy even when images are inside

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

# Step 2: keep the last N images (counting every pushed image, tagged or not) and expire the rest, so
# storage cost stays flat no matter how often main is pushed. N >= 2 leaves something to roll back to.
resource "aws_ecr_lifecycle_policy" "this" {
  for_each = aws_ecr_repository.this

  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last ${var.ecr_keep_last_images} images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = var.ecr_keep_last_images
      }
      action = { type = "expire" }
    }]
  })
}
