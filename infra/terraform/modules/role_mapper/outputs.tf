# The function ARN: the identity module attaches it to the user pool as the pre-token-generation trigger.
output "function_arn" { value = aws_lambda_function.this.arn }
output "function_name" { value = aws_lambda_function.this.function_name }
