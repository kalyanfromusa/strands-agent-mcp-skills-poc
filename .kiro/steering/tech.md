# Tech
- Python 3.12, `uv`. Strands Agents (built-in `AgentSkills` plugin, `MCPClient`), `bedrock-agentcore` SDK, FastMCP (`mcp` package).
- Model: Bedrock `us.anthropic.claude-haiku-5-5` (configurable).
- AWS: AgentCore Runtime (direct code deploy, arm64), AgentCore Gateway (MCP, CUSTOM_JWT/Cognito), AgentCore Identity (OAuth2 credential provider), Lambda, DynamoDB on-demand, S3, CloudWatch.
- IaC: Terraform (`hashicorp/aws` >= 6.71) **and** boto3 scripts, both producing `.deploy/outputs.json`.
- Tests: `uv run pytest` (moto for DynamoDB). No Postgres, no UI.
