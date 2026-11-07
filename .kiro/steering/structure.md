# Structure
agent/ (generic agent) · skills/ (SKILL.md folders) · mcp_servers/{common,orders_lambda,support_mcp} · identity/role_mapper/ (Cognito pre-token Lambda) · onprem_api/ (simulated on-prem warehouse API) · data/ · scripts/ · infra/{terraform/{bootstrap,platform,modules,envs},policies} · .github/workflows/ · tests/ · docs/
Contracts (names, schemas, env vars) are in `.kiro/specs/strands-poc/design.md`. Change them there first.
Specs: `.kiro/specs/strands-poc/` (original PoC, done) · `.kiro/specs/prod-deploy/` (production delivery, current work queue: `prod-deploy-tasks.md`).
