# =====================================================================================================
# modules/memory: one shared AgentCore Memory with two built-in long-term strategies
#
# What this module does
#   Creates an AgentCore Memory store and attaches a SEMANTIC strategy (facts and context pulled out
#   of conversations) and a SUMMARIZATION strategy (a summary per session). Built-in strategies need
#   no execution role and no model configuration. Short-term events expire after event_expiry_days.
#
# Notes
#   Memory names allow letters, digits and underscores only (no hyphens), so the name is
#   <name_prefix with hyphens replaced>_shared_memory, e.g. shopassist_prod_shared_memory.
#   Namespaces start with /actors/{actorId}/ so the agent can search one user's memories by prefix
#   (/actors/<id>/semantic/ and /actors/<id>/summaries/) without knowing the strategy ids.
#   The agent (agent/app.py) gets the memory id as MEMORY_ID and uses the Strands session manager.
# =====================================================================================================

resource "aws_bedrockagentcore_memory" "this" {
  name                  = "${replace(var.name_prefix, "-", "_")}_shared_memory"
  description           = "ShopAssist shared memory (semantic facts and session summaries)."
  event_expiry_duration = var.event_expiry_days
}

resource "aws_bedrockagentcore_memory_strategy" "semantic" {
  name                = "semantic"
  memory_id           = aws_bedrockagentcore_memory.this.id
  type                = "SEMANTIC"
  description         = "Facts and context extracted from conversations."
  namespace_templates = ["/actors/{actorId}/semantic/"]
}

resource "aws_bedrockagentcore_memory_strategy" "summarization" {
  name                = "summarization"
  memory_id           = aws_bedrockagentcore_memory.this.id
  type                = "SUMMARIZATION"
  description         = "One summary per session."
  namespace_templates = ["/actors/{actorId}/summaries/{sessionId}/"]

  # The service allows one strategy change at a time; create the second after the first.
  depends_on = [aws_bedrockagentcore_memory_strategy.semantic]
}
