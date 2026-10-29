# Requirements
1. A generic Strands agent with no domain logic. All behaviour comes from skills loaded at runtime.
2. Skills call tools only through an MCP server (AgentCore Gateway). They declare which servers they use.
3. Tools: order lookup, return eligibility and return creation (Lambda target), plus tickets and FAQ (MCP server target on AgentCore Runtime).
4. The agent runs on AgentCore Runtime and calls the Gateway with the signed-in user's own Cognito token (passthrough). AgentCore Identity (Cognito M2M) authenticates the Gateway to the support MCP runtime.
5. Deployable and destroyable with Terraform **or** boto3, with clear instructions and a comparison.
6. Very low cost: serverless, on-demand, short log retention, teardown scripts.
7. Docs: use case, agent capabilities, technical architecture, deployment guides, cost, extending with skills, troubleshooting.
8. Adding a new skill (a folder uploaded to S3) changes the agent's behaviour without a code change.
9. Per-user access control: alice (all tools), bob (read-only tools), carol (no agent access), enforced by Runtime JWT claims and AgentCore Policy at the Gateway, not by the agent.
