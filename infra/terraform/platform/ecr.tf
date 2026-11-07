# =====================================================================================================
# platform/ecr.tf: the four container image repositories, shared by every environment
#
# What this file does
#   Creates one private ECR repository per workload image (agent, support MCP server, orders Lambda,
#   role-mapper Lambda), with immutable tags, a vulnerability scan on every push, and a cleanup rule.
#
# Where it fits
#   GitHub Actions' deploy.yml pushes images tagged with the full git commit SHA. The
#   main stack (each environment) points its runtimes and Lambdas at <repo>:<sha>. Because the repos
#   are shared, the image tested in dev is byte-for-byte the image promoted to prod.
#
# Step by step
#   1. One repository per entry in local.repos.
#   2. Tags are IMMUTABLE: once <sha> is pushed it can never be overwritten, so a deployed SHA always
#      means the same bytes (good for audits and rollbacks).
#   3. Scan on push: ECR checks each new image for known vulnerabilities (basic scanning, free).
#   4. A lifecycle rule keeps only the newest 2 images: the one running now and the previous one, for
#      rollback. This is safe because images are pushed ONLY by the deploy job, right before it deploys
#      them (merges build and scan, but don't push). If a deploy fails after pushing, the job deletes
#      the image it just pushed, so the two kept images are always "current" and "previous".
#      ECR runs the cleanup asynchronously (usually within 24 hours).
#
# Key terms
#   ECR (Elastic Container Registry): AWS's private store for container images.
#   Image tag: a label on an image; here always the 40-character git commit SHA.
#   force_delete: lets `terraform destroy` delete a repository that still holds images.
# =====================================================================================================

locals {
  # Logical name => repository name (design.md section 1 and 5).
  repos = {
    agent         = "${var.name_prefix}-agent"
    support_mcp   = "${var.name_prefix}-support-mcp"
    orders_lambda = "${var.name_prefix}-orders-lambda"
    role_mapper   = "${var.name_prefix}-role-mapper"
  }
}

# Steps 1-3: the repositories.
resource "aws_ecr_repository" "this" {
  for_each = local.repos
  #checkov:skip=CKV_AWS_136:AES256 at rest is used; a customer KMS key would add cost without changing who can pull.

  name                 = each.value
  image_tag_mutability = "IMMUTABLE" # a pushed SHA tag can never be re-pointed
  force_delete         = true        # cost hygiene: destroy works even with images inside

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

# Step 4: keep only the newest N images per repository.
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
