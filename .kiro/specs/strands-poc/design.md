# ShopAssist: Design & Contracts

This file is the **contract between components**. If you change a name, schema or env var here, every
consumer must change too. The architecture narrative lives in `docs/03-architecture.md`.

> **Revision 2 (2026-11-06), production delivery.** Terraform is the only deploy path (boto3 is removed). All
> workloads ship as arm64 container images in ECR (no zips). Names are environment-aware (`dev` | `prod`), the access
> groups are renamed to `Group-ShopAssist-*`, Entra ID is federated (§9), and an on-prem API target is added (§10).
> The plan and the work queue are in `.kiro/specs/prod-deploy/`.

## 1. Naming

`<env>` is `dev` or `prod` (Terraform variable `environment`). Both environments can live in one account side by side.
Only `prod` is deployed.

| Thing | Value |
|---|---|
| Project prefix / tags | `shopassist`. Tags on every taggable resource: `project=shopassist`, `environment=<env>` |
| Region / account | `us-east-1` (variable), deploy account `438750401181` (from caller identity) |
| Model ID | `us.anthropic.claude-haiku-4-5-20251001-v1:0` (variable `model_id`) |
| Artifacts bucket | `shopassist-<env>-artifacts-<account>-<region>` (holds `skills/` only, no code, no versioning) |
| DynamoDB tables | `shopassist-<env>-orders` (PK `order_id`, GSI `customer_id-index` on `customer_id`), `shopassist-<env>-returns` (PK `return_id`), `shopassist-<env>-tickets` (PK `ticket_id`). All PAY_PER_REQUEST |
| Orders Lambda | `shopassist-<env>-orders-tools` (container image, arm64, CMD `handler.lambda_handler`) |
| Role-mapper Lambda | `shopassist-<env>-role-mapper` (container image, arm64; the Cognito pre-token-generation trigger, §9) |
| Agent runtime | `shopassist_<env>_agent` (Runtime names: `[a-zA-Z][a-zA-Z0-9_]{0,47}`, no hyphens). Protocol HTTP |
| MCP runtime | `shopassist_<env>_support_mcp`. Protocol MCP |
| Gateway | `shopassist-<env>-gateway`, protocol MCP, authorizer CUSTOM_JWT, no semantic search |
| Gateway targets | `orders` (Lambda), `support` (MCP server = the MCP runtime), `onprem` (OpenAPI, §10). Target names do **not** carry the env, so tool names are identical in every env |
| Policy engine | `shopassist_<env>_policy_engine` |
| Cognito | user pool `shopassist-<env>-pool`, domain `shopassist-<env>-<account>` (prod also gets the custom sign-in domain `strands-agents-poc.nalamone.com`, P7), resource servers `shopassist` (scope `invoke` → `shopassist/invoke`) and `onprem` (scope `read` → `onprem/read`). App clients: `shopassist-<env>-m2m` (client_credentials, secret), `shopassist-<env>-users` (public, §8 + §9), `shopassist-<env>-onprem` (client_credentials, secret, §10) |
| Cognito groups | `Group-ShopAssist-Admin`, `Group-ShopAssist-Viewer` (§8) |
| OAuth2 credential providers (AgentCore Identity) | `shopassist-<env>-cognito` (M2M client; Gateway→support MCP), `shopassist-<env>-onprem` (onprem client; Gateway→on-prem API) |
| IAM roles | `shopassist-<env>-agent-runtime-role`, `-support-mcp-runtime-role`, `-gateway-role`, `-orders-tools-role`, `-role-mapper-role` |
| Shared (per account, not per env) | ECR repos `shopassist-agent`, `shopassist-support-mcp`, `shopassist-orders-lambda`, `shopassist-role-mapper`; state bucket `shopassist-tfstate-<account>-<region>`; GitHub OIDC provider; CI roles `shopassist-gha-plan`, `shopassist-gha-apply-dev`, `shopassist-gha-apply-prod` (no separate build role: only the deploy job pushes images); permissions boundaries `shopassist-<env>-boundary` (every main-stack IAM role must carry its env's boundary) |
| Log retention | 7 days |

Gateway tool names are `<target>___<tool>` (three underscores), e.g. `orders___get_order`.

## 2. MCP tools (the only place business rules are *enforced*)

### Target `orders` (Lambda; schemas in `mcp_servers/orders_lambda/tools_schema.json`, Gateway inline-schema format: a list of `{name, description, inputSchema:{type:object, properties, required}}`)
| Tool | Input | Output (JSON) |
|---|---|---|
| `get_order` | `order_id: str` | order object or `{"error": "ORDER_NOT_FOUND"}` |
| `list_customer_orders` | `customer_id: str` | `{"customer_id", "orders": [order summary: order_id, status, order_date, total]}` |
| `check_return_eligibility` | `order_id: str`, `sku: str` | `{"eligible": bool, "reason": str, "days_since_delivery": int\|null, "return_window_days": int, "refund_amount": float}` |
| `create_return` | `order_id: str`, `sku: str`, `reason: str` | `{"return_id": "RET-xxxxxx", "status": "REQUESTED", "refund_amount", "label_url"}` or `{"error": "NOT_ELIGIBLE", "reason"}` (re-checks eligibility server-side) |

Order object: `order_id, customer_id, status (PROCESSING|SHIPPED|DELIVERED|CANCELLED), order_date (ISO date), delivered_date (ISO date|null), carrier, tracking_number, items: [{sku, name, category, qty, price, returned: bool}], total`.

Eligibility rules (implemented in the tool, *described* in the skill's `references/return-policy.md`):
- the order must be `DELIVERED`; the item must exist and not already be returned
- category `final-sale` → never eligible
- window: `electronics` 15 days, everything else 30 days, counted from `delivered_date`
- refund_amount = price × qty

Lambda tool routing: the tool name comes from `context.client_context.custom["bedrockAgentCoreToolName"]`; strip everything up to and including `___`. Event = the tool arguments dict.

### Target `support` (MCP server built with the `mcp` 2.x SDK, `mcp_servers/support_mcp/server.py`, stateless streamable HTTP on `0.0.0.0:8000/mcp`)
| Tool | Input | Output |
|---|---|---|
| `create_ticket` | `customer_id: str, subject: str, description: str, order_id: str = "", priority: "low"\|"medium"\|"high" = "medium"` | `{"ticket_id": "TCK-xxxxxx", "status": "OPEN", "created_at"}` |
| `get_ticket` | `ticket_id: str` | ticket object or `{"error": "TICKET_NOT_FOUND"}` |
| `search_faq` | `query: str` | `{"results": [{"question","answer"}]}` (top 3, keyword match over the bundled `faq.json`) |

### Target `onprem`: see §10.

### Data layer
`mcp_servers/common/store.py`: `get_store()` returns `MemoryStore` (seeded from `data/seed.json`) when `STORE=memory`, else `DynamoStore` using env `ORDERS_TABLE`, `RETURNS_TABLE`, `TICKETS_TABLE`. It is copied into the orders Lambda image and the support MCP image (as package `common`).

`data/seed.json` uses **relative dates** (`order_days_ago`, `delivered_days_ago`) so the return-window demos always work. `scripts/seed_data.py --region R --env <env>` converts them to ISO dates and writes to DynamoDB (idempotent put). Customers `C001`–`C003`; ≈10 orders covering: in-window clothing, out-of-window, electronics at 10 and 20 days, final-sale, shipped (not delivered), an already-returned item. Order ids `1001…`. Demo anchor: order `1042` (C001, delivered 5 days ago, wireless headphones, electronics → eligible).

## 3. Generic agent (`agent/`): zero domain knowledge
- `app.py`: `BedrockAgentCoreApp`, `@app.entrypoint` receives `{"prompt": str}` and returns `{"response": str, "skills_used": [..], "tools_used": [..]}`. One Strands `Agent` per process (Runtime = one microVM per session ⇒ conversation memory per session). Generic system prompt only.
- Skills: Strands built-in `strands.vended_plugins.skills.AgentSkills`. Source: if `SKILLS_S3_URI` is set → sync `s3://…/skills/` to `/tmp/skills` at cold start; else `SKILLS_DIR` (default `./skills`).
- Extra generic tool `read_skill_resource(skill_name, path)`: reads a file inside an activated skill dir (path-traversal safe).
- **Hybrid skill→tool binding**: SKILL.md frontmatter `metadata: { mcp-servers: "orders support" }` (space-separated). Instructions mention *plain* tool names (`create_return`).
  - After the `skills` tool activates a skill, a hook appends to its result the resolved tool list for the declared servers (`orders___create_return — <description>`).
  - `BeforeToolCallEvent` hook: a Gateway tool (`<server>___<tool>`) is allowed only if an activated skill declares `<server>`; otherwise `cancel_tool`, with a message telling the model to activate the right skill. Non-gateway tools (skills, read_skill_resource) are always allowed.
  - Startup validation: warn for declared servers with no tools on the Gateway, and for backticked plain tool names in a skill body that don't exist on its declared servers.
- Gateway connection: Strands `MCPClient` (streamable HTTP) to `GATEWAY_URL`, `Authorization: Bearer <token>`; the token is refreshed before expiry.

Env vars (agent):
| Var | Meaning |
|---|---|
| `MODEL_ID` | Bedrock model / inference profile |
| `AWS_REGION` | region |
| `GATEWAY_URL` | Gateway MCP URL (`https://<id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp`) or local `http://localhost:<port>/mcp` |
| `GATEWAY_AUTH_MODE` | `passthrough` (deployed: forward the caller's Authorization header, see §8) · `client_credentials` (local dev: `COGNITO_TOKEN_URL`, `COGNITO_CLIENT_ID`, `COGNITO_CLIENT_SECRET`) · `none` (local emulator) |
| `OAUTH_PROVIDER_NAME` | `shopassist-<env>-cognito` |
| `OAUTH_SCOPES` | `shopassist/invoke` |
| `SKILLS_S3_URI` / `SKILLS_DIR` | skills source |

Env vars (support MCP runtime): `STORE`, `TICKETS_TABLE`, `AWS_REGION`. Orders Lambda: `STORE`, `ORDERS_TABLE`, `RETURNS_TABLE`. Role mapper: §9.

## 4. Local dev
`scripts/local_gateway.py`: a FastMCP server on `localhost:<port>/mcp` (default 8000; use `--port 8765` where 8000 is taken) that exposes **the same prefixed names** as the real Gateway (`orders___*` calling the Lambda handler functions, `support___*` calling the support server functions, `onprem___*` calling the on-prem API functions in-process) with `STORE=memory`. `--role admin|viewer` filters its tool list like Cedar does. `scripts/run_local.py`: an interactive chat with the generic agent using `GATEWAY_AUTH_MODE=none`, `SKILLS_DIR=./skills` and real Bedrock. `onprem_api/` runs locally for real (§10).

## 5. Packaging: container images (replaces the zips)
All images are **linux/arm64**, built **only by GitHub Actions** on native arm64 runners (no local Docker) with `docker buildx build --platform linux/arm64 --provenance=false`, using the repo root as context (allow-list `.dockerignore` per image), and tagged **only** with the full 40-hex git SHA. ECR tags are immutable and scan on push is on. **Each repository keeps exactly 2 images: the current one and the previous one** (lifecycle rule `imageCountMoreThan 2`). That's why images are pushed **only by the deploy job**, right before deploying (PR and merge builds don't push), and why a deploy that fails after pushing deletes the image it pushed. Rollback goes back one image. Images are built once and promoted between envs by SHA.

| Image (ECR repo) | Dockerfile | Base | Entry point | Port |
|---|---|---|---|---|
| `shopassist-agent` | `agent/Dockerfile` | `python:3.12-slim`, non-root uid 10001 | `app.py` | 8080 (`/invocations`, `/ping`) |
| `shopassist-support-mcp` | `mcp_servers/support_mcp/Dockerfile` | `python:3.12-slim`, non-root uid 10001 | `server.py` (+ `common/`) | 8000 (`/mcp`) |
| `shopassist-orders-lambda` | `mcp_servers/orders_lambda/Dockerfile` | `public.ecr.aws/lambda/python:3.12-arm64` | CMD `handler.lambda_handler` (+ `common/`) | n/a |
| `shopassist-role-mapper` | `identity/role_mapper/Dockerfile` | `public.ecr.aws/lambda/python:3.12-arm64` | CMD `handler.lambda_handler` | n/a |

Dependencies are **pinned**: each image installs a requirements file exported from `uv.lock` (`uv export --frozen --no-hashes`). A test fails if an exported file drifts from the lock.

## 6. Deployment outputs (written by Terraform)
`.deploy/outputs.json` (git-ignored), one per deployed env (the CLI scripts take `--env`, default `prod`, and read `.deploy/<env>/outputs.json`):
```json
{"region","account_id","environment","image_tag","artifacts_bucket","skills_s3_uri","agent_runtime_arn","support_mcp_runtime_arn",
 "gateway_id","gateway_url","cognito_user_pool_id","cognito_client_id","cognito_token_url","oauth_provider_name",
 "cognito_users_client_id","cognito_domain_url","policy_engine_id","deployed_by":"terraform"}
```
Users file: `.deploy/<env>/users.json` (demo users, §8). `scripts/invoke_agent.py` reads the outputs (or `--arn`) and calls the agent runtime over HTTPS with a user token (§8) and a ≥33-char `runtimeSessionId`.

## 7. Wiring details
- MCP runtime inbound auth: JWT authorizer, discovery URL `https://cognito-idp.<region>.amazonaws.com/<pool_id>/.well-known/openid-configuration`, `allowed_clients=[m2m client id]`.
- Gateway inbound auth: the same discovery URL, `allowed_clients=[users client id, m2m client id]` (§8).
- Gateway target `support` endpoint: `https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<url-encoded runtime ARN>/invocations?qualifier=DEFAULT`, outbound OAuth via provider `shopassist-<env>-cognito`, scope `shopassist/invoke`.
- Gateway target `orders`: Lambda ARN + inline tool schema, outbound credential = the gateway IAM role.
- Gateway target `onprem`: §10.
- After an image change of the support runtime (or a change to the on-prem OpenAPI spec), Terraform re-synchronizes the affected Gateway targets (`terraform_data` + `local-exec` `SynchronizeGatewayTargets`), as part of `apply`.
- IAM: gateway role (invoke Lambda, AgentCore Identity token APIs, read the provider secrets); agent runtime role (Bedrock invoke, S3 read skills, workload access token APIs, logs, X-Ray, CloudWatch metrics, ECR pull of its image; no provider secret, since the agent forwards the user token per §8); MCP runtime role (DynamoDB tickets CRUD, logs, ECR pull); orders Lambda role (DynamoDB orders/returns, logs); role-mapper role (logs only).

## 8. Per-user access control (who can use the agent and which tools)

Showcase: **one agent, one Gateway, many users, three outcomes**, enforced *outside* the agent code. Users are either
Cognito-native (the demo users) or federated from Entra ID (§9). Both end up with the same `cognito:groups` claim.

| Cognito group | Agent access | Tools |
|---|---|---|
| `Group-ShopAssist-Admin` | ✅ | all (`orders___*`, `support___*`, `onprem___*`) |
| `Group-ShopAssist-Viewer` | ✅ | read-only: `orders___get_order`, `orders___list_customer_orders`, `orders___check_return_eligibility`, `support___get_ticket`, `support___search_faq`, `onprem___get_stock`, `onprem___get_restock_eta` (no `create_return`, no `create_ticket`) |
| none | ❌ HTTP 401/403 at the Runtime, before any agent code runs | — |

Demo users (Cognito-native, in the pool): `alice` → `Group-ShopAssist-Admin`, `bob` → `Group-ShopAssist-Viewer`, `carol` → no group.

Identity flow:
1. Cognito: the **public app client** `shopassist-<env>-users` (no secret). Flows: `ALLOW_USER_PASSWORD_AUTH` + `ALLOW_REFRESH_TOKEN_AUTH` (demo users), plus OAuth authorization code with **PKCE** through managed login (Entra users and the browser sign-in, §9). Access token lifetime 15 min, refresh token 8 h. Users alice/bob/carol get random permanent passwords. Credentials go to `.deploy/<env>/users.json` (git-ignored): `{"alice": {"username","password","group"}, ...}`. The M2M client `shopassist-<env>-m2m` is used only for Gateway→support-MCP outbound OAuth and for local dev.
2. The client (`scripts/invoke_agent.py --user alice`, or `--login` for a browser sign-in) gets an **access token** (it carries `cognito:groups`). It calls the agent runtime over HTTPS: `POST https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<urlencoded ARN>/invocations?qualifier=DEFAULT` with `Authorization: Bearer <token>`, `Content-Type: application/json` and the header `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: <≥33 chars>`. (A runtime with a JWT authorizer doesn't accept SigV4.)
3. **Layer 1: the agent runtime's inbound JWT authorizer.** Cognito discovery URL, `allowed_clients=[users client id]`, plus a **custom claim** rule: `cognito:groups` (string array) contains any of `["Group-ShopAssist-Admin","Group-ShopAssist-Viewer"]`. carol and ungrouped Entra users get a 401/403.
4. The agent runtime has `request_header_configuration.request_header_allowlist = ["Authorization"]`, so the agent receives the user's token. Agent `GATEWAY_AUTH_MODE=passthrough` forwards that same token to the Gateway (per request, via a contextvar read by the httpx.Auth).
5. **Layer 2: the Gateway's inbound JWT authorizer.** `allowed_clients=[users client id, m2m client id]` (m2m is kept so that local dev works).
6. **Layer 3: AgentCore Policy** (Cedar), policy engine `shopassist_<env>_policy_engine` attached to the Gateway with `mode = "ENFORCE"`. Default deny, and **`tools/list` is filtered per principal**. Principal type `AgentCore::OAuthUser`. JWT claims are string tags (arrays become JSON text, so use `like`). Policies:
```cedar
// admins: everything on this gateway
permit(principal is AgentCore::OAuthUser, action, resource is AgentCore::Gateway)
when { principal.hasTag("cognito:groups") && principal.getTag("cognito:groups") like "*\"Group-ShopAssist-Admin\"*" };

// viewers: read-only tools
permit(principal is AgentCore::OAuthUser,
  action in [AgentCore::Action::"orders___get_order", AgentCore::Action::"orders___list_customer_orders",
             AgentCore::Action::"orders___check_return_eligibility", AgentCore::Action::"support___get_ticket",
             AgentCore::Action::"support___search_faq", AgentCore::Action::"onprem___get_stock",
             AgentCore::Action::"onprem___get_restock_eta"],
  resource == AgentCore::Gateway::"<gateway ARN>")
when { principal.hasTag("cognito:groups") && principal.getTag("cognito:groups") like "*\"Group-ShopAssist-Viewer\"*" };

// local dev / M2M client: everything (M2M tokens have no groups; match on client_id)
permit(principal is AgentCore::OAuthUser, action, resource is AgentCore::Gateway)
when { principal.hasTag("client_id") && principal.getTag("client_id") == "<m2m client id>" };
```
   The policy files live as templates in `infra/policies/*.cedar` (placeholders `{{GATEWAY_ARN}}` and `{{M2M_CLIENT_ID}}`), rendered by Terraform (`replace(file(...))`). Order: policy engine → gateway (with `policy_engine_configuration`) → targets → policies. Check the gateway role permissions needed for policy evaluation at https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-permissions.html.
7. **Layer 4 (soft, in the agent):** the agent lists the Gateway tools *with the caller's token*, so it only sees what the caller may use. The skill-binding resolution text marks a skill's tools that aren't available, e.g. "Not available to the current user: create_return". The skill instructions then say: if a needed tool isn't available, explain that the user's account lacks permission and offer what you *can* do. No warnings for missing tools in passthrough mode (that's expected); log at info level. The agent caches its tool list per token `sub`, and rebuilds the MCP client and agent if the `sub` changes.

Demo: `scripts/demo_access.py` runs the matrix: (a) alice asks to return a 1042 item and it succeeds; (b) bob asks the same, and the agent explains he lacks permission (`create_return` is absent from his tools); (c) carol gets a 401/403; (d) a **direct Gateway call** with bob's token to `tools/call orders___create_return` is denied by policy, proving enforcement doesn't depend on the agent behaving. Optional Entra scenarios e1–e3 (§9) need a browser sign-in.
Local emulator: `scripts/local_gateway.py --role admin|viewer` filters its tool list the same way.

## 9. Entra ID federation (rights from directory groups)

The chain: **AD group → Entra app role → Cognito group**. Membership is maintained only in the directory group.

| Layer | Lives in | Admin | Viewer |
|---|---|---|---|
| `AD-` security group (members) | Entra ID (cloud-only groups; an on-prem AD synced by Cloud Sync works the same) | `AD-ShopAssist-<Env>-Admins` | `AD-ShopAssist-<Env>-Viewers` |
| `AppRole-` app role (value) | Entra app registration `ShopAssist-<Env>` (`<Env>` = `Dev` / `Prod`) | `AppRole-ShopAssist-Admin` | `AppRole-ShopAssist-Viewer` |
| `Group-` Cognito group (claim) | Cognito pool, `cognito:groups` | `Group-ShopAssist-Admin` | `Group-ShopAssist-Viewer` |

- **Entra** (manual, runbook `docs/12-entra-federation.md`): app registration `ShopAssist-<Env>`, redirect URI `https://<cognito domain>/oauth2/idpresponse`, a client secret, the app roles above (allowed member types Users/Groups). In the enterprise app, the `AD-` groups are assigned to their app roles, with **Assignment required = Yes**. Needs Entra ID P1/P2 (trial) for group assignment.
- **Cognito IdP** `EntraID`: type OIDC, issuer `https://login.microsoftonline.com/<tenant id>/v2.0`, `authorize_scopes = "openid email profile"`, client id from a variable, client secret from `TF_VAR_entra_client_secret` (never committed). Attribute mapping: `email` → `email`, `name` → `name`, `roles` → `custom:entra_roles` (custom attribute, mutable, string, max 2048). The users client lists `supported_identity_providers = ["COGNITO", "EntraID"]`.
- **Role mapper** `shopassist-<env>-role-mapper`, the pre-token-generation trigger. Env vars:

| Var | Meaning |
|---|---|
| `ENTRA_ROLE_SOURCE` | `roles` (default: read `custom:entra_roles`) or `groups` (Entra Free fallback: read `custom:entra_groups`, mapped from the `groups` claim; keys are group object ids) |
| `ENTRA_ROLE_MAP` | JSON, e.g. `{"AppRole-ShopAssist-Admin":"Group-ShopAssist-Admin","AppRole-ShopAssist-Viewer":"Group-ShopAssist-Viewer"}` (Terraform variable `entra_role_map`) |
| `ENTRA_PROVIDER_NAME` | `EntraID`; only users of this provider are mapped |

  Rules: Cognito-native users pass through unchanged. For federated users, the claim values are parsed (a JSON array or a comma-separated string), only keys in the map count (an allow-list), and the **highest** role wins (Admin > Viewer), producing a single group. With no match, the groups are empty (fail closed). The trigger overrides `cognito:groups` in the ID **and access** tokens (verify that V1 `groupOverrideDetails` reaches the access token; otherwise use V2 `accessTokenGeneration`, which needs the Essentials tier). It logs the user `sub`, the source and the decision, never the tokens.
- **Revocation:** remove the user from the `AD-` group. It takes effect at the next sign-in or refresh (access token 15 min). Immediate: disable the user in Entra plus Cognito `AdminUserGlobalSignOut`.
- **Clients:** `scripts/invoke_agent.py --login` and `scripts/chat_ui.py` "Sign in" use managed login, authorization code + PKCE, `state` + `nonce` checks, a loopback redirect `http://localhost:8790/callback` (registered as a callback URL with the logout URL `http://localhost:8790/`).
- Demo scenarios: e1 an AD admin → all tools; e2 an AD viewer → read-only; e3 an Entra user in no group → blocked; plus revocation (move e1 to Viewers → `create_return` gone after re-sign-in).

## 10. On-prem API (simulated) and Gateway target `onprem`

- **Service:** `onprem_api/`, FastAPI "warehouse system", run on the developer's machine, binding `127.0.0.1:8770`. Read-only, with its own seed data (`onprem_api/data/stock.json`) for the SKUs in `data/seed.json`. The OpenAPI spec is served at `/openapi.json`, with a committed copy at `onprem_api/openapi.json` used by Terraform.

| Tool (operationId) | HTTP | Input | Output |
|---|---|---|---|
| `get_stock` | `GET /stock/{sku}` | `sku: str` | `{"sku", "name", "on_hand": int, "reserved": int, "available": int, "warehouse", "updated_at"}` or 404 `{"error": "SKU_NOT_FOUND"}` |
| `get_restock_eta` | `GET /restock-eta/{sku}` | `sku: str` | `{"sku", "next_restock_date": ISO date\|null, "quantity": int\|null}` or 404 `{"error": "SKU_NOT_FOUND"}` |

  Gateway names: `onprem___get_stock`, `onprem___get_restock_eta`.
- **Reachability:** a Cloudflare named tunnel (`cloudflared`, outbound only) → `https://onprem-<env>.nalamone.com` → `127.0.0.1:8770`. Terraform variable `onprem_api_base_url` (per env). There's no inbound port on the developer network. (This replaces ngrok, decided 2026-11-07.)
- **Gateway target `onprem`:** type OpenAPI (the spec from `onprem_api/openapi.json`, `servers[0].url` = `onprem_api_base_url`), outbound OAuth2 client credentials via the provider `shopassist-<env>-onprem` (client `shopassist-<env>-onprem`, scope `onprem/read`).
- **API security** (env vars):

| Var | Meaning |
|---|---|
| `ONPREM_ISSUER` | `https://cognito-idp.<region>.amazonaws.com/<pool id>` (JWKS = `<issuer>/.well-known/jwks.json`) |
| `ONPREM_ALLOWED_CLIENT_IDS` | comma-separated; the onprem client id |
| `ONPREM_REQUIRED_SCOPE` | `onprem/read` |
| `ONPREM_PORT` | `8770` |
| `ONPREM_RATE_LIMIT` | requests per minute per client (default 60) |
| `ONPREM_AUTH_DISABLED` | `true` only for `local_gateway.py` in-process use; never honoured when bound to anything but 127.0.0.1 behind the tunnel (the server refuses to start with auth disabled unless `--local-only`) |

  Each request is checked: RS256 signature via JWKS (cached, refreshed on an unknown `kid`), `iss`, `token_use == "access"`, `client_id` in the allow-list, the scope present, `exp`/`iat`. Failures → 401 (missing/invalid) or 403 (wrong client/scope), with no detail in the body. Plus a request size limit, rate limiting, a JSON audit log line per request (time, client_id, operation, sku, status, latency; never the token) and generic 500s.
- **Skill:** `skills/inventory-check/SKILL.md`, `metadata.mcp-servers: "onprem"`. If the tool errors or times out: say the warehouse system is unreachable right now, never guess stock numbers.

## 11. Delivery: Terraform roots and CI/CD

| Root | Contents | State |
|---|---|---|
| `infra/terraform/bootstrap/` | the state bucket `shopassist-tfstate-<account>-<region>` (no versioning, SSE-S3, public access blocked, TLS-only) | local, git-ignored |
| `infra/terraform/platform/` | the ECR repos, the GitHub OIDC provider, the CI roles (§1) | S3 `platform.tfstate`, `use_lockfile = true` |
| `infra/terraform/` (main) | everything per env | S3 `main/<env>.tfstate`, `use_lockfile = true`, `-backend-config=envs/<env>.backend.hcl` |

Main-stack variables added: `environment` (`dev`\|`prod`), `image_tag` (40-hex, required), `entra_tenant_id`, `entra_client_id`, `entra_client_secret` (sensitive, via `TF_VAR_`), `entra_role_source`, `entra_role_map`, `enable_entra` (bool), `onprem_api_base_url`, `enable_onprem` (bool). Per-env values: `envs/<env>.tfvars` (committed, no secrets). Destroy order: main → platform → bootstrap.

CI roles (GitHub OIDC, repo `kalyanfromusa/strands-agent-mcp-skills-poc`, public):
- `shopassist-gha-plan`: read-only + state read/lock write. Trust `sub` ∈ {`repo:<repo>:pull_request`, `repo:<repo>:ref:refs/heads/main`}.
- `shopassist-gha-apply-<env>`: least privilege on `shopassist-<env>-*` resources (Cognito by tags `project`/`environment`), ECR push/delete on the 4 repos, its own state + lock. It may create IAM roles only with the boundary `shopassist-<env>-boundary`. Trust `sub = repo:<repo>:environment:<env>` only.

Destroy: `destroy.yml` (manual: `target_env`, typed `confirm` = `destroy <env>`, `delete_images`; plan -destroy → approval → re-plan hash check → destroy → `scripts/leftovers.py --sweep`, which fails if anything named `shopassist-<env>-*`/`shopassist_<env>_*` or tagged `project`+`environment` remains). The apply role gets a second policy, `shopassist-gha-sweep-<env>` (list calls + emptying ECR), because a managed policy may hold at most 6,144 characters. Full teardown: `scripts/destroy_everything.sh` (local: envs → platform → bootstrap → `leftovers.py --all`).

Workflows: `ci.yml` (PR: tests, IaC checks, image builds without push, a summary-only plan comment, skipped for forks), `build.yml` (push to `main`: tests, build, scan, **no push, no deploy**), `deploy.yml` (manual `workflow_dispatch` only: inputs `target_env`, `image_tag`; plan → GitHub environment approval → build + push the images if that SHA isn't in ECR yet → apply of that exact plan → smoke; on failure, delete images pushed by this run). Logs never show full plan or apply output (public repo). GitHub environment `prod` has you as required reviewer; `dev` is disabled by `DEPLOY_DEV=false`.

AWS profiles: the deploy account is used through the profile `personal`, per command. The Claude session itself never uses it.
