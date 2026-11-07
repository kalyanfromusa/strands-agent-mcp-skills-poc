# Product
ShopAssist is a low-cost PoC: a **generic, skills-driven Strands agent** for a fictional online store's customer support
(order status, returns/refunds, support tickets, warehouse stock from a simulated on-prem system). The agent has no business logic. Skills (`skills/*/SKILL.md`) hold the
procedures, and MCP tools behind Amazon Bedrock AgentCore Gateway hold data and rule enforcement.
Users sign in with Cognito or with Microsoft Entra ID; their directory groups decide what they may do.
Audience: architects and engineers evaluating AgentCore Runtime, Gateway, Identity and Policy, with production-style
delivery (Terraform, container images, GitHub Actions).
