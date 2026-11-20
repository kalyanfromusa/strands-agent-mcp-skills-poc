# =====================================================================================================
# modules/gateway/outputs.tf: what the Gateway module hands on
#
# What this file does
#   Publishes the Gateway's id, ARN and URL, and the policy engine's id and ARN.
#
# Where it fits
#   The root main.tf passes gateway_url to module "agent_runtime" (the agent's GATEWAY_URL setting),
#   uses gateway_id and support_target_name for the target re-sync (root step 6b), and writes
#   gateway_id, gateway_url and policy_engine_id to .deploy/<env>/outputs.json.
#
# Step by step
#   1. Report the Gateway id and ARN, and the support target's name.
#   2. Report the Gateway URL, but only once both targets and all policies exist.
#   3. Report the policy engine id and ARN.
#
# Key terms
#   Gateway URL: the HTTPS address the agent sends its MCP tool requests to.
# =====================================================================================================

output "gateway_id" { value = aws_bedrockagentcore_gateway.this.gateway_id }

# The support target's name, so the root stack can re-synchronize it after its server's image changes.
output "support_target_name" { value = aws_bedrockagentcore_gateway_target.support.name }

# The depends_on below holds back this value, and so delays everything that uses it (the agent
# runtime), until the Gateway is fully set up.
output "gateway_url" {
  # Consumers (the agent runtime) wait until targets and policies are in place,
  # otherwise the agent's startup tool listing would see an empty gateway.
  value = aws_bedrockagentcore_gateway.this.gateway_url
  depends_on = [
    aws_bedrockagentcore_gateway_target.orders,
    aws_bedrockagentcore_gateway_target.support,
    aws_bedrockagentcore_policy.this,
  ]
}

output "policy_engine_id" { value = aws_bedrockagentcore_policy_engine.this.policy_engine_id }
