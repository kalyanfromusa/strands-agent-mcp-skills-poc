# =====================================================================================================
# modules/data/outputs.tf: what the storage module hands to the other modules
#
# What this file does
#   Publishes the bucket name, the skills location,
#   and the names and ARNs of the three DynamoDB tables.
#
# Where it fits
#   The root main.tf reads these as module.data.<name> and passes them to lambda_tools, mcp_runtime
#   and agent_runtime. Because those modules use these values, Terraform creates storage first.
#
# Step by step
#   1. Report the bucket.
#   2. Report the skills location, only once all skills files are uploaded.
#   3. Report the table names (for the code's settings) and ARNs (for permission rules).
#
# Key terms
#   ARN (Amazon Resource Name): the unique id of an AWS resource, used in permission rules.
# =====================================================================================================

# The bucket name and ARN.
output "artifacts_bucket" { value = aws_s3_bucket.artifacts.id }
output "artifacts_bucket_arn" { value = aws_s3_bucket.artifacts.arn }

# The folder (key prefix) inside the bucket where the skills are stored.
output "skills_prefix" { value = "skills/" }

# The full skills address the agent reads at start-up, e.g. s3://<bucket>/skills/.
output "skills_s3_uri" {
  # Depends on the uploads so the agent never starts against an empty prefix.
  value      = "s3://${aws_s3_bucket.artifacts.id}/skills/"
  depends_on = [aws_s3_object.skill]
}

# Table names go into the code's settings; ARNs go into the permission rules (IAM policies).
output "orders_table_name" { value = aws_dynamodb_table.orders.name }
output "orders_table_arn" { value = aws_dynamodb_table.orders.arn }
output "returns_table_name" { value = aws_dynamodb_table.returns.name }
output "returns_table_arn" { value = aws_dynamodb_table.returns.arn }
output "tickets_table_name" { value = aws_dynamodb_table.tickets.name }
output "tickets_table_arn" { value = aws_dynamodb_table.tickets.arn }
