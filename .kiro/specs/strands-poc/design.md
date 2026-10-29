# ShopAssist — Design & Contracts

This file is the **contract between components**. If you change a name, schema, or env var here, every
consumer must change too. Architecture narrative lives in `docs/03-architecture.md`.

## 1. Naming

| Thing | Value |
|---|---|
| Project prefix / tag | `shopassist` / tag `project=shopassist` on every taggable resource |
| Region / account | `us-east-1` (variable), account from caller identity |
| Model ID | `us.anthropic.claude-haiku-5-5` (variable `model_id`) |
| Artifacts bucket | `shopassist-artifacts-<account>-<region>` (zips under `artifacts/`, skills under `skills/`) |
| DynamoDB tables | `shopassist-orders` (PK `order_id`, GSI `customer_id-index` on `customer_id`), `shopassist-returns` (PK `return_id`), `shopassist-tickets` (PK `ticket_id`) — all PAY_PER_REQUEST |
| Lambda | `shopassist-orders-tools` (python3.12, arm64, handler `handler.lambda_handler`) |
| Agent runtime | `shopassist_agent` (Runtime names: `[a-zA-Z][a-zA-Z0-9_]{0,47}`, no hyphens) — protocol HTTP |
| MCP runtime | `shopassist_support_mcp` — protocol MCP |
| Gateway | `shopassist-gateway`, protocol MCP, authorizer CUSTOM_JWT, no semantic search |
| Gateway targets | `orders` (Lambda), `support` (MCP server = the MCP runtime) |
| Cognito | user pool `shopassist-pool`, domain `shopassist-<account>`, resource server id `shopassist` with scope `invoke` → full scope `shopassist/invoke`, M2M app client `shopassist-m2m` (client_credentials, generate secret) |
| OAuth2 credential provider (AgentCore Identity) | `shopassist-cognito` (vendor CustomOauth2, Cognito discovery URL, client id/secret). Used by agent→gateway AND gateway→support-mcp |
| Log retention | 7 days |

Gateway tool names are `<target>___<tool>` (three underscores), e.g. `orders___get_order`.

## 2. MCP tools (the only place business rules are *enforced*)

### Target `orders` (Lambda; schemas in `mcp_servers/orders_lambda/tools_schema.json`, Gateway inline-schema format: list of `{name, description, inputSchema:{type:object, properties, required}}`)
| Tool | Input | Output (JSON) |
|---|---|---|
| `get_order` | `order_id: str` | order object or `{"error": "ORDER_NOT_FOUND"}` |
| `list_customer_orders` | `customer_id: str` | `{"customer_id", "orders": [order summary: order_id, status, order_date, total]}` |
| `check_return_eligibility` | `order_id: str`, `sku: str` | `{"eligible": bool, "reason": str, "days_since_delivery": int\|null, "return_window_days": int, "refund_amount": float}` |
| `create_return` | `order_id: str`, `sku: str`, `reason: str` | `{"return_id": "RET-xxxxxx", "status": "REQUESTED", "refund_amount", "label_url"}` or `{"error": "NOT_ELIGIBLE", "reason"}` (re-checks eligibility server-side) |

Order object: `order_id, customer_id, status (PROCESSING|SHIPPED|DELIVERED|CANCELLED), order_date (ISO date), delivered_date (ISO date|null), carrier, tracking_number, items: [{sku, name, category, qty, price, returned: bool}], total`.

Eligibility rules (implemented in the tool, *described* in the skill's `references/return-policy.md`):
- order must be `DELIVERED`; item must exist and not already be returned
- category `final-sale` → never eligible
- window: `electronics` 15 days, everything else 30 days, counted from `delivered_date`
- refund_amount = price × qty

Lambda tool routing: tool name from `context.client_context.custom["bedrockAgentCoreToolName"]`, strip everything up to and including `___`. Event = tool arguments dict.

### Target `support` (MCP server built with the `mcp` 2.x SDK, `mcp_servers/support_mcp/server.py`, stateless streamable HTTP on `0.0.0.0:8000/mcp`)
| Tool | Input | Output |
|---|---|---|
| `create_ticket` | `customer_id: str, subject: str, description: str, order_id: str = "", priority: "low"\|"medium"\|"high" = "medium"` | `{"ticket_id": "TCK-xxxxxx", "status": "OPEN", "created_at"}` |
| `get_ticket` | `ticket_id: str` | ticket object or `{"error": "TICKET_NOT_FOUND"}` |
| `search_faq` | `query: str` | `{"results": [{"question","answer"}]}` (top 3, keyword match over bundled `faq.json`) |

### Data layer
`mcp_servers/common/store.py` — `get_store()` returns `MemoryStore` (seeded from `data/seed.json`) when `STORE=memory`, else `DynamoStore` using env `ORDERS_TABLE`, `RETURNS_TABLE`, `TICKETS_TABLE`. Copied into both the Lambda zip and the MCP runtime zip by `scripts/build.py`.

`data/seed.json` uses **relative dates** (`order_days_ago`, `delivered_days_ago`) so return-window demos always work; `scripts/seed_data.py --region R` converts to ISO dates and writes to DynamoDB (idempotent put). Customers `C001`–`C003`; ≈10 orders covering: in-window clothing, out-of-window, electronics at 10 and 20 days, final-sale, shipped (not delivered), already-returned item. Order ids `1001…`. Demo anchor: order `1042` (C001, delivered 5 days ago, wireless headphones, electronics → eligible).

## 3. Generic agent (`agent/`) — zero domain knowledge
- `app.py`: `BedrockAgentCoreApp`, `@app.entrypoint` receives `{"prompt": str}`; returns `{"response": str, "skills_used": [..], "tools_used": [..]}`. One Strands `Agent` per process (Runtime = one microVM per session ⇒ conversation memory per session). Generic system prompt only.
- Skills: Strands built-in `strands.vended_plugins.skills.AgentSkills`. Source: if `SKILLS_S3_URI` set → sync `s3://…/skills/` to `/tmp/skills` at cold start; else `SKILLS_DIR` (default `./skills`).
- Extra generic tool `read_skill_resource(skill_name, path)` — reads a file inside an activated skill dir (path-traversal safe).
- **Hybrid skill→tool binding**: SKILL.md frontmatter `metadata: { mcp-servers: "orders support" }` (space-separated). Instructions mention *plain* tool names (`create_return`).
  - After the `skills` tool activates a skill, a hook appends to its result the resolved tool list for the declared servers (`orders___create_return — <description>`).
  - `BeforeToolCallEvent` hook: a Gateway tool (`<server>___<tool>`) is allowed only if an activated skill declares `<server>`; otherwise `cancel_tool` with a message telling the model to activate the right skill. Non-gateway tools (skills, read_skill_resource) always allowed.
  - Startup validation: warn for declared servers with no tools on the Gateway and for backticked plain tool names in a skill body that don't exist on its declared servers.
- Gateway connection: Strands `MCPClient` (streamable HTTP) to `GATEWAY_URL`, `Authorization: Bearer <token>`; token refreshed before expiry.

Env vars (agent):
| Var | Meaning |
|---|---|
| `MODEL_ID` | Bedrock model / inference profile |
| `AWS_REGION` | region |
| `GATEWAY_URL` | Gateway MCP URL (`https://<id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp`) or local `http://localhost:8000/mcp` |
| `GATEWAY_AUTH_MODE` | `passthrough` (deployed: forward the caller's Authorization header, see §8) · `client_credentials` (local dev: `COGNITO_TOKEN_URL`, `COGNITO_CLIENT_ID`, `COGNITO_CLIENT_SECRET`) · `none` (local emulator) |
| `OAUTH_PROVIDER_NAME` | `shopassist-cognito` |
| `OAUTH_SCOPES` | `shopassist/invoke` |
| `SKILLS_S3_URI` / `SKILLS_DIR` | skills source |

Env vars (support MCP runtime): `STORE`, `TICKETS_TABLE`, `AWS_REGION`. Lambda: `STORE`, `ORDERS_TABLE`, `RETURNS_TABLE`.

## 4. Local dev
`scripts/local_gateway.py` — FastMCP server on `localhost:8000/mcp` that exposes **the same prefixed names** as the real Gateway (`orders___*` calling the Lambda handler functions, `support___*` calling support server functions) with `STORE=memory`. `scripts/run_local.py` — interactive chat with the generic agent using `GATEWAY_AUTH_MODE=none`, `SKILLS_DIR=./skills`, real Bedrock.

## 5. Packaging (`scripts/build.py` → `dist/`)
- `dist/agent.zip` — `agent/*` + deps from `agent/requirements.txt`, at zip root, wheels for **linux aarch64, cp312** (`uv pip install --target … --python-platform aarch64-manylinux2014 --python-version 3.12 --only-binary=:all:`). Entry point `app.py`.
- `dist/support_mcp.zip` — `mcp_servers/support_mcp/*` + `mcp_servers/common/` (as package `common`) + deps, same platform. Entry point `server.py`.
- `dist/orders_lambda.zip` — `handler.py` + `common/` (boto3 from Lambda runtime; no deps).
- Zips must use forward-slash paths (built on Windows).

## 6. Deployment outputs (both paths write the same file)
`.deploy/outputs.json`:
```json
{"region","account_id","artifacts_bucket","skills_s3_uri","agent_runtime_arn","support_mcp_runtime_arn",
 "gateway_id","gateway_url","cognito_user_pool_id","cognito_client_id","cognito_token_url","oauth_provider_name","deployed_by":"terraform|boto3"}
```
`scripts/invoke_agent.py` reads it (or `--arn`) and calls `bedrock-agentcore:InvokeAgentRuntime` (SigV4) with a ≥33-char `runtimeSessionId`.

## 7. Wiring details
- MCP runtime inbound auth: JWT authorizer, discovery URL `https://cognito-idp.<region>.amazonaws.com/<pool_id>/.well-known/openid-configuration`, `allowed_clients=[client_id]`.
- Gateway inbound auth: same discovery URL / allowed clients.
- Gateway target `support` endpoint: `https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<url-encoded runtime ARN>/invocations?qualifier=DEFAULT`, outbound OAuth via provider `shopassist-cognito`, scope `shopassist/invoke`.
- Gateway target `orders`: Lambda ARN + inline tool schema, outbound credential = gateway IAM role.
- **Superseded in part by §8:** the agent runtime and Gateway inbound auth use the *user* app client. The agent forwards the user's token (passthrough) instead of using its own M2M token.
- IAM: gateway role (invoke Lambda, AgentCore Identity token APIs, read provider secret); agent runtime role (Bedrock invoke, S3 read skills, workload access token APIs, logs, X-Ray, CloudWatch metrics; no provider secret, since the agent forwards the user token per §8); MCP runtime role (DynamoDB tickets CRUD, logs); Lambda role (DynamoDB orders/returns, logs).

## 8. Per-user access control (who can use the agent and which tools)

Showcase: **one agent, one Gateway, three users, three outcomes**, enforced *outside* the agent code.

| Demo user (Cognito) | Group | Agent access | Tools |
|---|---|---|---|
| `alice` | `shop-admin` | ✅ | all 7 |
| `bob` | `shop-viewer` | ✅ | read-only: `orders___get_order`, `orders___list_customer_orders`, `orders___check_return_eligibility`, `support___get_ticket`, `support___search_faq` (no `create_return`, no `create_ticket`) |
| `carol` | none | ❌ HTTP 401/403 at the Runtime, before any agent code runs | — |

Identity flow (replaces agent-M2M for the deployed agent):
1. Cognito: add a **public app client** `shopassist-users` (no secret, `ALLOW_USER_PASSWORD_AUTH` + `ALLOW_REFRESH_TOKEN_AUTH`). Add groups `shop-admin` and `shop-viewer`, and users alice/bob/carol with random permanent passwords. Credentials go to `.deploy/users.json` (git-ignored): `{"alice": {"username","password","group"}, ...}`. The M2M client `shopassist-m2m` stays, used only by Gateway→support-MCP outbound OAuth and for local dev.
2. The client (`scripts/invoke_agent.py --user alice`) gets an **access token** (USER_PASSWORD_AUTH; it carries `cognito:groups`). It calls the agent runtime over HTTPS: `POST https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<urlencoded ARN>/invocations?qualifier=DEFAULT` with `Authorization: Bearer <token>`, `Content-Type: application/json`, and header `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: <≥33 chars>`. (A runtime with a JWT authorizer doesn't accept SigV4.)
3. **Layer 1: agent runtime inbound JWT authorizer.** Cognito discovery URL, `allowed_clients=[users client id]`, plus a **custom claim** rule: `cognito:groups` (string array) contains any of `["shop-admin","shop-viewer"]`. carol gets a 401/403.
4. The agent runtime `request_header_configuration.request_header_allowlist = ["Authorization"]`, so the agent receives the user's token. Agent `GATEWAY_AUTH_MODE=passthrough` forwards that same token to the Gateway (per request, via a contextvar read by the httpx.Auth).
5. **Layer 2: Gateway inbound JWT authorizer.** `allowed_clients=[users client id, m2m client id]` (m2m kept so that local dev works).
6. **Layer 3: AgentCore Policy** (Cedar), policy engine `shopassist_policy_engine` attached to the Gateway with `mode = "ENFORCE"`. Default deny, and **`tools/list` is filtered per principal**. Principal type `AgentCore::OAuthUser`; JWT claims are string tags (arrays become JSON text, so use `like`). Policies:
```cedar
// admins: everything on this gateway
permit(principal is AgentCore::OAuthUser, action, resource is AgentCore::Gateway)
when { principal.hasTag("cognito:groups") && principal.getTag("cognito:groups") like "*\"shop-admin\"*" };

// viewers: read-only tools
permit(principal is AgentCore::OAuthUser,
  action in [AgentCore::Action::"orders___get_order", AgentCore::Action::"orders___list_customer_orders",
             AgentCore::Action::"orders___check_return_eligibility", AgentCore::Action::"support___get_ticket",
             AgentCore::Action::"support___search_faq"],
  resource == AgentCore::Gateway::"<gateway ARN>")
when { principal.hasTag("cognito:groups") && principal.getTag("cognito:groups") like "*\"shop-viewer\"*" };

// local dev / M2M client: everything (M2M tokens have no groups; match on client_id)
permit(principal is AgentCore::OAuthUser, action, resource is AgentCore::Gateway)
when { principal.hasTag("client_id") && principal.getTag("client_id") == "<m2m client id>" };
```
   Policy files live as templates in `infra/policies/*.cedar` (placeholders `{{GATEWAY_ARN}}` and `{{M2M_CLIENT_ID}}`), shared by Terraform (`replace(file(...))`) and boto3 (`str.replace`). Order: policy engine → gateway (with `policy_engine_configuration`) → targets → policies. Check the gateway role permissions needed for policy evaluation at https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-permissions.html.
7. **Layer 4 (soft, in the agent):** the agent lists Gateway tools *with the caller's token*, so it only sees what the caller may use. The skill-binding resolution text marks a skill's tools that aren't available, e.g. "Not available to the current user: create_return". The skill instructions then say: if a needed tool isn't available, explain that the user's account lacks permission and offer what you *can* do. No warnings for missing tools in passthrough mode (that's expected); log at info level. The agent caches its tool list per token `sub` and rebuilds the MCP client and agent if the `sub` changes.

Outputs additions (`.deploy/outputs.json`): `cognito_users_client_id`, `policy_engine_id`. The users file is `.deploy/users.json`.
Demo: `scripts/demo_access.py` runs the matrix: (a) alice asks to return a 1042 item and it succeeds; (b) bob asks the same, the agent explains he lacks permission (and `create_return` is absent from his tools); (c) carol gets a 401/403; (d) **direct Gateway call** with bob's token to `tools/call orders___create_return` is denied by policy, proving enforcement doesn't depend on the agent behaving.
Local emulator: `scripts/local_gateway.py --role admin|viewer` filters its tool list the same way, for testing the agent's handling locally.
