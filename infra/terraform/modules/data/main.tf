# =====================================================================================================
# modules/data/main.tf: storage for ShopAssist (skills files in S3, business data in DynamoDB)
#
# What this file does
#   Creates the private S3 bucket that holds the skills files, uploads them, creates the three DynamoDB
#   tables (orders, returns, tickets), and optionally loads the demo orders. (The code no longer lives
#   here: it ships as container images in ECR.)
#
# Where it fits
#   Called first by the root main.tf (module "data"). Its outputs feed the other modules: the table
#   names go to lambda_tools (orders, returns) and mcp_runtime (tickets); the skills location goes to
#   agent_runtime.
#
# Step by step
#   1. Create the S3 bucket <prefix>-artifacts-<account>-<region> (prefix includes the environment).
#   2. No versioning (decision 2026-11-07): an upload replaces the file.
#   3. Block all public access to the bucket.
#   4. (Removed: code zips. The code is in container images now.)
#   5. Upload every file of the skills/ folder under skills/.
#   6. Create the DynamoDB tables <prefix>-orders, <prefix>-returns and <prefix>-tickets.
#   7. If seed_data = true, run scripts/seed_data.py on your computer to fill the orders table.
#
# Key terms
#   S3 (Simple Storage Service): AWS file storage. A "bucket" is a top-level folder; a "key" is a path.
#   DynamoDB: an AWS database of tables of items (rows), each found by its key (e.g. order_id).
#   GSI (Global Secondary Index): a second way to look up a DynamoDB table, here by customer_id.
#   etag: a fingerprint of a file's content; when it changes, Terraform uploads the file again.
#   Seed data: the demo records (orders such as 1042) loaded so the PoC has something to show.
# =====================================================================================================

# Bucket names are global across all AWS customers, so the account id and region make it unique.
locals {
  bucket_name = "${var.name_prefix}-artifacts-${var.account_id}-${var.region}"
}

# ---------------------------------------------------------------- S3 artifacts

# Step 1: the bucket that holds the skills files. The agent reads them at cold start.
resource "aws_s3_bucket" "artifacts" {
  bucket        = local.bucket_name
  force_destroy = true # cost hygiene: `terraform destroy` must not stop at a non-empty bucket
}

# Step 3: make sure nothing in the bucket can ever be made public. Only AWS roles we grant can read it.
resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Step 5: mirror every file in skills/ (the SKILL.md procedures) to s3://<bucket>/skills/.
# The content type tells readers whether a file is Markdown, JSON or plain text.
# Skills are plain files read by the agent at cold start (SKILLS_S3_URI), so a skill
# edit only needs `terraform apply` + a new session, not a new agent image.
resource "aws_s3_object" "skill" {
  for_each = fileset(var.skills_dir, "**")

  bucket = aws_s3_bucket.artifacts.id
  key    = "skills/${each.value}"
  source = "${var.skills_dir}/${each.value}"
  etag   = filemd5("${var.skills_dir}/${each.value}")
  content_type = (
    endswith(each.value, ".md") ? "text/markdown" :
    endswith(each.value, ".json") ? "application/json" : "text/plain"
  )

}

# ---------------------------------------------------------------- DynamoDB

# Step 6a: the orders table, one item per order, looked up by order_id (e.g. "1042").
# PAY_PER_REQUEST ("on-demand") means no fixed monthly fee: you pay per read and write,
# which is close to zero for a demo. Only key columns are declared; DynamoDB is schema-free otherwise.
resource "aws_dynamodb_table" "orders" {
  name         = "${var.name_prefix}-orders"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "order_id"

  attribute {
    name = "order_id"
    type = "S"
  }

  attribute {
    name = "customer_id"
    type = "S"
  }

  # list_customer_orders queries by customer instead of scanning.
  global_secondary_index {
    name            = "customer_id-index"
    projection_type = "ALL"

    # key_schema replaces the deprecated GSI hash_key argument (provider >= 6.x).
    key_schema {
      attribute_name = "customer_id"
      key_type       = "HASH"
    }
  }
}

# Step 6b: the returns table, one item per return request created by the create_return tool.
resource "aws_dynamodb_table" "returns" {
  name         = "${var.name_prefix}-returns"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "return_id"

  attribute {
    name = "return_id"
    type = "S"
  }
}

# Step 6c: the support tickets table, used by the support tools create_ticket and get_ticket.
resource "aws_dynamodb_table" "tickets" {
  name         = "${var.name_prefix}-tickets"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "ticket_id"

  attribute {
    name = "ticket_id"
    type = "S"
  }
}

# ---------------------------------------------------------------- Seed data

# Step 7 (optional): run scripts/seed_data.py on the computer that runs Terraform (a "local-exec"
# provisioner), to write the demo orders from data/seed.json into the orders table.
# terraform_data is an empty placeholder resource that exists only to run this command. Its
# triggers_replace values mean: run the script again when data/seed.json changes, or when a
# table is re-created (new ARN). count = 0 (seed_data = false) skips it entirely.
# The seed script turns relative dates into real ones, so it runs locally rather than
# as aws_dynamodb_table_item resources (which would freeze the dates at plan time).
# The command has no quotes, env syntax or pipes, so it works the same under
# cmd.exe (local-exec default on Windows) and /bin/sh (Linux/macOS).
resource "terraform_data" "seed" {
  count = var.seed_data ? 1 : 0

  triggers_replace = {
    seed_hash = fileexists(var.seed_file) ? filemd5(var.seed_file) : "missing"
    tables    = join(",", [aws_dynamodb_table.orders.arn, aws_dynamodb_table.returns.arn, aws_dynamodb_table.tickets.arn])
  }

  provisioner "local-exec" {
    working_dir = var.repo_root
    command     = "uv run python scripts/seed_data.py --region ${var.region} --prefix ${var.name_prefix}"
  }
}
