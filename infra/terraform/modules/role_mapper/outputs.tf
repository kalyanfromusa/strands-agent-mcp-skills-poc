# =====================================================================================================
# modules/role_mapper/outputs.tf: what the role-mapper module hands on
#
# What this file does
#   Publishes the role-mapper Lambda's ARN and name.
#
# Where it fits
#   The root main.tf passes function_arn to module "identity" (as role_mapper_arn), which attaches the
#   function to the Cognito user pool as its pre-token-generation trigger and lets the pool call it.
#   Because the module only exists with enable_entra = true, the root reads it as
#   module.role_mapper[0].function_arn.
#
# Step by step
#   1. Report the function ARN and name once the function exists.
#
# Key terms
#   ARN (Amazon Resource Name): the unique id of an AWS resource.
# =====================================================================================================

# The function ARN: the identity module attaches it to the user pool as the pre-token-generation trigger.
output "function_arn" { value = aws_lambda_function.this.arn }
