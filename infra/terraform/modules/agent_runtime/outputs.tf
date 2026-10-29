# =====================================================================================================
# modules/agent_runtime/outputs.tf: what the agent runtime module hands on
#
# What this file does
#   Publishes the agent runtime's ARN and id, and the name of its log group.
#
# Where it fits
#   The root main.tf writes runtime_arn to .deploy/outputs.json as agent_runtime_arn, which is the
#   address scripts/invoke_agent.py calls.
#
# Step by step
#   1. Report the runtime ARN and id once the runtime is created.
#   2. Report the log group name, handy when looking for the agent's logs in CloudWatch.
#
# Key terms
#   ARN (Amazon Resource Name): the unique id of an AWS resource.
# =====================================================================================================

output "runtime_arn" { value = aws_bedrockagentcore_agent_runtime.this.agent_runtime_arn }
output "runtime_id" { value = aws_bedrockagentcore_agent_runtime.this.agent_runtime_id }
output "log_group" { value = aws_cloudwatch_log_group.runtime.name }
