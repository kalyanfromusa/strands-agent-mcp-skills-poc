# Decisions
- 2026-10-29 Use case: ShopAssist e-commerce support.
- 2026-10-29 MCP tools via both a Lambda Gateway target (`orders`) and a FastMCP server on AgentCore Runtime (`support`).
- 2026-10-29 Both Terraform and boto3 deployments, with a comparison doc. Deploy only one at a time.
- 2026-10-29 Model: Claude Haiku on Bedrock (`us.anthropic.claude-haiku-5-5`).
- 2026-10-29 Skill→tool binding is hybrid: a skill declares `metadata.mcp-servers` and its instructions use plain tool names. The agent resolves names to `<server>___<tool>` and blocks undeclared servers.
- 2026-10-29 Use the Strands built-in `AgentSkills` plugin rather than a custom loader.
- 2026-10-29 Direct-code (zip) Runtime deployment, so no ECR or Docker is needed.
- 2026-10-29 Per-user access: Runtime JWT custom-claim check (who may use the agent), user token passed through to the Gateway, AgentCore Policy in Cedar (which tools each user may call; tools/list is filtered). Demo users alice (admin), bob (viewer), carol (no access).
