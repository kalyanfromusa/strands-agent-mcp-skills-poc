# Requirements
1. A generic Strands agent with no domain logic. All behaviour comes from skills loaded at runtime.
2. Skills call tools only through an MCP server (AgentCore Gateway). They declare which servers they use.
3. Tools: order lookup, return eligibility and return creation (Lambda target), tickets and FAQ (MCP server target on AgentCore Runtime), and warehouse stock from a simulated on-prem API (OpenAPI target reached through a secure tunnel).
4. The agent runs on AgentCore Runtime and calls the Gateway with the signed-in user's own Cognito token (passthrough). AgentCore Identity (Cognito M2M) authenticates the Gateway to the support MCP runtime.
5. Deployable and destroyable with **Terraform only** (the boto3 path was retired on 2026-11-06), from container images in ECR, through a GitHub Actions pipeline with a manual, approved deploy. Environments dev and prod are defined; prod is deployed.
6. Very low cost: serverless, on-demand, short log retention, teardown scripts.
7. Docs: use case, agent capabilities, technical architecture, deployment guides, cost, extending with skills, troubleshooting.
8. Adding a new skill (a folder uploaded to S3) changes the agent's behaviour without a code change.
9. Per-user access control: alice (all tools), bob (read-only tools), carol (no agent access), enforced by Runtime JWT claims and AgentCore Policy at the Gateway, not by the agent.
10. Federated sign-in from Microsoft Entra ID alongside Cognito users. Rights come from directory groups (AD group → Entra app role → Cognito group), with no AWS change when membership changes.
