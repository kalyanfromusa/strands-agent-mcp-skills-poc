# =====================================================================================================
# modules/lambda_tools/outputs.tf: what the orders Lambda module hands on
#
# What this file does
#   Publishes the Lambda function's ARN and name.
#
# Where it fits
#   The root main.tf passes function_arn to module "gateway", which uses it for the `orders` target
#   and for the Gateway role's permission to call the function.
#
# Step by step
#   1. Report the function ARN and name once the function exists.
#
# Key terms
#   ARN (Amazon Resource Name): the unique id of an AWS resource.
# =====================================================================================================

output "function_arn" { value = aws_lambda_function.this.arn }
output "function_name" { value = aws_lambda_function.this.function_name }
