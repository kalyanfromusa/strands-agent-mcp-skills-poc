# 3. Architecture

The contracts (names, schemas, env vars) are in `.kiro/specs/strands-poc/design.md`. This page
explains how the pieces fit together and why. `<env>` is `dev` or `prod` (only prod is deployed);
diagrams show the prod names.

## Components

| Component | AWS service | Code | Role |
|---|---|---|---|
| Client CLI | (your machine) | `scripts/invoke_agent.py` | Signs a demo user in to Cognito and calls the agent runtime over HTTPS with the user's Bearer token |
| Agent `shopassist_<env>_agent` | AgentCore Runtime, protocol HTTP | `agent/` | Generic Strands agent: AgentSkills plugin + MCP client to the Gateway + Bedrock model |
| Skills | S3 `s3://shopassist-<env>-artifacts-<account>-<region>/skills/` | `skills/` | Procedures, read by the agent at cold start |
| Model | Amazon Bedrock | n/a | `us.anthropic.claude-haiku-4-5-20251001-v1:0` (Claude Haiku 4.5 inference profile, variable `model_id`) |
| Gateway `shopassist-<env>-gateway` | AgentCore Gateway, protocol MCP, CUSTOM_JWT | n/a | One MCP endpoint over two targets, Cedar policy enforcement |
| Target `orders` | Lambda `shopassist-<env>-orders-tools` (container image, python3.12, arm64) | `mcp_servers/orders_lambda/` | `get_order`, `list_customer_orders`, `check_return_eligibility`, `create_return` |
| Target `support` | AgentCore Runtime `shopassist_<env>_support_mcp`, protocol MCP | `mcp_servers/support_mcp/` | FastMCP server: `create_ticket`, `get_ticket`, `search_faq` |
| Policy engine `shopassist_<env>_policy_engine` | AgentCore Policy | `infra/policies/*.cedar` | Default-deny Cedar policies, per-user tool filtering |
| Credential provider `shopassist-<env>-cognito` | AgentCore Identity | n/a | Holds the M2M client secret; the Gateway uses it to get tokens for the `support` target |
| User pool `shopassist-<env>-pool` | Amazon Cognito | n/a | Users client (alice/bob/carol), M2M client, groups, `shopassist/invoke` scope |
| Tables | DynamoDB on-demand | `mcp_servers/common/store.py` | `shopassist-<env>-orders` (GSI `customer_id-index`), `shopassist-<env>-returns`, `shopassist-<env>-tickets` |
| Logs | CloudWatch Logs (7 days) | n/a | Runtime and Lambda log groups |

```mermaid
flowchart LR
    U["User CLI<br/>scripts/invoke_agent.py"] -- "USER_PASSWORD_AUTH" --> COG[("Cognito<br/>shopassist-prod-pool")]
    U -- "HTTPS + Bearer (user token)" --> AR

    subgraph RT["AgentCore Runtime"]
        AR["shopassist_prod_agent<br/>Strands + AgentSkills"]
        MR["shopassist_prod_support_mcp<br/>FastMCP"]
    end

    AR -- "cold start: sync skills/" --> S3[("S3 artifacts<br/>skills/")]
    AR -- "InvokeModel" --> BR["Bedrock<br/>Claude Haiku"]
    AR -- "MCP + same Bearer (passthrough)" --> GW

    subgraph GWB["AgentCore Gateway: shopassist-prod-gateway"]
        GW["JWT authorizer"] --> POL{"AgentCore Policy<br/>Cedar, ENFORCE"}
        POL --> T1["target orders"]
        POL --> T2["target support"]
    end

    T1 -- "Invoke (gateway IAM role)" --> L["Lambda<br/>shopassist-prod-orders-tools"]
    T2 -- "MCP + M2M token" --> MR
    T2 -.->|"client_credentials token"| ID["AgentCore Identity<br/>shopassist-prod-cognito"]
    ID -.->|"token endpoint"| COG
    L --> D1[("DynamoDB<br/>orders, returns")]
    MR --> D2[("DynamoDB<br/>tickets")]
```

## Request flow

```mermaid
sequenceDiagram
    autonumber
    participant C as invoke_agent.py
    participant CG as Cognito
    participant R as Agent Runtime
    participant A as Agent (app.py)
    participant B as Bedrock
    participant G as Gateway
    participant L as orders Lambda
    C->>CG: InitiateAuth USER_PASSWORD_AUTH (users client)
    CG-->>C: access token (cognito:groups)
    C->>R: POST /runtimes/ARN/invocations?qualifier=DEFAULT<br/>Authorization: Bearer, Session-Id header, JSON prompt
    R->>R: JWT authorizer: client + cognito:groups claim
    R->>A: entrypoint(payload, context) with Authorization header
    Note over A: first call in this microVM:<br/>sync skills from S3, connect MCPClient,<br/>tools/list with the user's token
    A->>B: Converse (system prompt + skills list + tools)
    B-->>A: tool_use skills returns-and-refunds
    A-->>B: instructions + resolved tool names
    B-->>A: tool_use orders___check_return_eligibility
    A->>G: tools/call (Bearer = user token)
    G->>G: JWT authorizer + Cedar AuthorizeAction
    G->>L: Invoke (event = arguments, tool name in client context)
    L-->>G: {"eligible": true, ...}
    G-->>A: result
    A->>B: tool result
    B-->>A: final text
    A-->>R: {"response", "skills_used", "tools_used"}
    R-->>C: 200 JSON
```

Notes:

- The session id header (`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`, at least 33 characters) picks
  the microVM. `invoke_agent.py` keeps one per user in `.deploy/session-<user>.txt`, so a
  conversation continues across runs. `--new-session` starts a new one.
- One Strands `Agent` lives per process, and the Runtime gives each session its own microVM. That
  gives you conversation memory per session without a memory store. Calls are serialised with a
  lock. See [Conversation memory](#conversation-memory) for how and how long it remembers.
- The response is `{"response": str, "skills_used": [...], "tools_used": [...]}`. The CLI prints the
  reply and then a dim `skills: ... | tools: ...` line.

### Hosted chat page flow

The page on CloudFront signs the user in itself and reaches the Runtime through the relay Lambda:

```mermaid
sequenceDiagram
    autonumber
    participant U as Browser (chat page)
    participant CF as CloudFront + S3
    participant CG as Cognito (web client)
    participant RL as chat relay (Lambda URL)
    participant R as Agent Runtime
    U->>CF: GET / (index.html, chat_auth.js, config.json)
    U->>CG: /oauth2/authorize (code + PKCE challenge, state, nonce)
    CG-->>U: redirect to the page with ?code&state
    U->>CG: /oauth2/token (code + verifier)
    CG-->>U: access token (cognito:groups), kept in memory
    U->>RL: POST {"prompt"} + Authorization: Bearer + Session-Id (CORS: page origin only)
    RL->>R: same headers and body, token unchanged
    R-->>RL: 200 JSON (JWT authorizer checks client + group)
    RL-->>U: {"status", "elapsed_ms", "result"}
```

From the Runtime on, the flow is the one above. The Cognito web client id is in the Runtime's and the
Gateway's allowed client lists. The relay has no AWS rights beyond writing its logs.

## Generic agent design

`agent/` contains no domain words. Everything specific comes from skill files and the Gateway's tool
list.

| File | Responsibility |
|---|---|
| `app.py` | `BedrockAgentCoreApp` entrypoint; builds the Strands `Agent` (Bedrock model, `MCPClient` to the Gateway, `read_skill_resource`, plugins `AgentSkills` + `SkillBinding`); generic system prompt; rebuilds the agent when the caller changes |
| `config.py` | `Settings.from_env()`: `MODEL_ID`, `AWS_REGION`, `GATEWAY_URL`, `GATEWAY_AUTH_MODE`, `SKILLS_S3_URI` / `SKILLS_DIR`, `COGNITO_*`, ... |
| `skill_source.py` | Sync `s3://.../skills/` to `/tmp/skills` (path-safe), or use `SKILLS_DIR` |
| `skill_binding.py` | Hybrid skill→tool binding hooks, startup validation, usage tracking |
| `gateway_auth.py` | `httpx.Auth` for the MCP connection: passthrough, client_credentials or none |
| `tools.py` | `read_skill_resource(skill_name, path)`, the only local tool |

### Skills with progressive disclosure

The agent uses the Strands built-in `strands.vended_plugins.skills.AgentSkills` plugin:

1. **Discovery.** Before each invocation the plugin adds an `<available_skills>` block (name +
   `description` of each skill) to the system prompt. Only that short text is always in context.
2. **Activation.** The model calls the `skills` tool with a skill name. The plugin returns the full
   `SKILL.md` instructions plus a list of the skill's resource files, and records the skill in
   `agent.state["agent_skills"]["activated_skills"]`.
3. **Resources.** The model reads `references/...` files on demand with `read_skill_resource`. It can
   only read files of activated skills, and path traversal is rejected.

### Hybrid tool binding

Skills must not hard-code Gateway details. A skill declares *which servers* it may use, and its text
uses *plain* tool names:

```yaml
---
name: returns-and-refunds
description: Use when a customer wants to return an item, ...
metadata:
  mcp-servers: "orders support"
---
... Call `check_return_eligibility` with the order id and sku ...
```

`SkillBinding` (a Strands plugin in `agent/skill_binding.py`) connects the two with three hooks:

| Hook | What it does |
|---|---|
| `AfterToolCallEvent` on `skills` | Appends a `[Tool binding]` block to the activation result: the exact Gateway names for the declared servers (`- orders___create_return — <description>`), plus `Not available to the current user: ...` for plain names the skill mentions that this caller doesn't have |
| `BeforeToolCallEvent` | A Gateway tool `<server>___<tool>` runs only if an activated skill declares `<server>`. Otherwise the call is cancelled with a message naming the skill(s) to activate. A plain name such as `create_return` gets a hint with the exact name. Non-Gateway tools (`skills`, `read_skill_resource`) are always allowed |
| `BeforeInvocationEvent` | Resets the per-invocation `skills_used` / `tools_used` lists |

At startup, `validate_skills` logs declared servers without tools and backticked tool names that
don't exist on a skill's declared servers. In passthrough mode these are logged at INFO, because
per-user filtering makes "missing" tools normal there. Otherwise they're logged as WARNING.

```mermaid
sequenceDiagram
    autonumber
    participant M as Model
    participant SK as AgentSkills plugin
    participant SB as SkillBinding
    participant G as Gateway (MCP)
    Note over M: system prompt lists skills (name + description)
    M->>SB: tool_use orders___get_order (no skill active)
    SB-->>M: cancelled: activate a skill that declares 'orders'<br/>(candidates: order-tracking, returns-and-refunds)
    M->>SK: tool_use skills returns-and-refunds
    SK-->>SB: SKILL.md instructions + resources
    SB-->>M: + [Tool binding] orders___get_order, orders___check_return_eligibility, ...<br/>Not available to the current user: (only if filtered)
    M->>SB: tool_use orders___check_return_eligibility
    SB->>G: allowed (server 'orders' is declared by an active skill)
    G-->>M: result
```

Why hybrid rather than putting full tool names in the skill: the skill stays portable (no target
prefix, no endpoint), the agent can still stop a skill from using servers it didn't declare, and the
model gets the exact names at the moment it needs them.

### Why the binding exists

Skills say `create_return`. The Gateway exposes `orders___create_return`, and which tools exist
depends on the caller. Something has to bridge the two:

| Option | What breaks |
|---|---|
| Full Gateway names in skills | Skills depend on Gateway target names. Renaming a target or moving a tool means editing every skill that mentions it |
| Plain names, and the model works out the mapping | Usually works, but not reliably: the model sometimes calls `create_return` as written and gets "unknown tool" |
| Plain names plus a resolver (`SkillBinding`) | One plugin to maintain |

What each part buys:

1. **The `[Tool binding]` block.** The most useful part. It gives the model exact names when it
   needs them, and it's what makes bob's experience work: his tool list has no `create_return`, but
   the returns skill still mentions it. "Not available to the current user: create_return" turns that
   gap into a clean "your account doesn't have permission", which `demo_access.py` scenario b checks
   for. Without it the skill points at a tool the model can't see.
2. **The pre-call check.** All 7 tools are registered for every request. The check makes the model
   activate a skill first, so it follows the skill's steps (for example eligibility before return). It
   also limits each skill to the servers it declares: a new skill uploaded to S3 that only declares
   `support` can't make the agent call `orders` tools. That matters because skills change behaviour
   without code review.
3. **Startup validation and usage tracking.** Typos in skill tool names show up in the logs.
   `skills_used` / `tools_used` feed the CLI's dim line and the `demo_access.py` pass conditions.

**It's a behaviour layer, not a security control.** It runs inside the agent process and guides the
model. Enforcement is outside the agent: Cedar hides and denies tools per user (scenario d), and
`create_return` re-checks eligibility server-side. Removing the binding wouldn't let bob do anything
new.

**Costs:** one plugin with three hooks tied to Strands' event API; the binding text is part of the
~800–1,000 tokens added on activation; and an occasional extra model call when a premature tool call
is cancelled and the model corrects itself.

**Could it go?** The pre-call check is the optional part, if you trust the model and the tools to
enforce step order. Dropping the whole plugin means full Gateway names in skills plus a generic
system-prompt rule ("if a tool a skill mentions isn't in your list, say the user lacks permission"):
simpler, but skills become tied to target names and the permission message depends on the model
following a general rule.

## Conversation memory

The agent remembers a conversation in process memory only. There is no memory store: the
"memory" is the message history of one Strands `Agent` object, and AgentCore Runtime keeps that
object alive by giving each session its own microVM.

```
session id ──► microVM ──► app.py process ──► AgentRuntime ──► Agent
                                                               ├─ messages  (conversation history)
                                                               └─ state     (activated skills)
```

Turn by turn:

1. **The client picks the conversation.** `invoke_agent.py` and the chat UI send
   `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`. The CLI keeps one id per user in
   `.deploy/session-<user>.txt`. The Runtime routes every request with that id to the same microVM.
2. **One agent per process.** The microVM runs one `app.py` process, which holds one `AgentRuntime`
   (`get_runtime()`), which holds one Strands `Agent` (`self._agent`).
3. **Built once, then reused.** The first message builds the agent (load skills, connect to the
   Gateway, list tools). After that `_agent_for()` returns the same object while the caller's token
   `sub` stays the same.
4. **Strands keeps the history.** `agent(prompt)` appends the user message to `agent.messages`, runs
   the model/tool loop, and appends the replies and tool results. The whole list goes to Bedrock on the
   next turn, so "and the other order?" makes sense.
5. **Skill state carries over.** Activated skills are recorded in `agent.state["agent_skills"]`, so a
   follow-up can use the returns skill without activating it again.

| Limit | Effect |
|---|---|
| Not persisted | When the microVM ends, the conversation is gone. Sessions end after the Runtime's idle timeout or maximum lifetime. This repo doesn't set them, so the AWS defaults apply (check the current AgentCore docs for the values). A redeploy also starts fresh microVMs |
| History capped at 40 messages | Strands' default `SlidingWindowConversationManager` keeps the last 40 messages. Tool calls and results count, so a return flow fills the window quickly |
| Changing user resets it | A different token `sub` in the same process makes `_agent_for()` discard the agent and build a new one. Normally each user has their own session id, so this doesn't come up |
| One conversation per process | Requests take turns behind `self._lock`. Fine when each session has its own microVM. Locally (`run_local.py`, or `app.py` with `GATEWAY_AUTH_MODE=none`) there's one conversation per process whatever session id you send |
| Cost grows per turn | The full history is re-sent on every model call ([07-cost.md](07-cost.md)) |
| Starting fresh | `--new-session`, `/new` or the chat UI's *New session*: a new id means a new microVM and an empty history |

**Memory across sessions** needs storage outside the microVM: AgentCore Memory (managed short- and
long-term memory), or a Strands session manager that saves `messages` and `state` to S3 or DynamoDB
keyed by session id. Either also survives idle timeouts and redeploys. Neither is part of this PoC.

## Per-user access control

One agent, one Gateway, three users. The deployed stack enforces access in four layers, and the
agent adds a fifth, soft layer:

| # | Layer | Where | Decides | carol | bob | alice |
|---|---|---|---|---|---|---|
| 1 | Runtime inbound JWT authorizer + custom claim | Agent runtime | Who may use the agent: token from the users client, `cognito:groups` CONTAINS_ANY `Group-ShopAssist-Admin`, `Group-ShopAssist-Viewer` | 403 | pass | pass |
| 2 | Token passthrough | Agent code (`GATEWAY_AUTH_MODE=passthrough`) | The Gateway sees the *user*, not the agent | n/a | bob's token | alice's token |
| 3 | Gateway inbound JWT authorizer | Gateway | Token is from the users client (or the M2M client, for local dev) | n/a | pass | pass |
| 4 | AgentCore Policy (Cedar), ENFORCE, default deny | Gateway | Which tools each principal can list and call; `tools/list` is filtered | n/a | 5 read-only tools | all 7 |
| 5 | Skill guidance | Skills + binding text | How the agent explains a missing permission | n/a | "your account doesn't have permission" | n/a |

```mermaid
sequenceDiagram
    autonumber
    participant U as User (alice / bob / carol)
    participant CG as Cognito
    participant R as Agent Runtime
    participant A as Agent code
    participant G as Gateway
    participant P as Policy engine (Cedar)
    U->>CG: USER_PASSWORD_AUTH (shopassist-prod-users client)
    CG-->>U: access token (client_id, sub, cognito:groups)
    U->>R: invoke, Authorization: Bearer
    alt carol: no allowed group
        R-->>U: 403 (layer 1, no agent code runs)
    else alice / bob
        R->>A: request + Authorization header (allowlisted)
        A->>G: tools/list, Bearer = same user token (layer 2)
        G->>G: JWT authorizer: allowed client (layer 3)
        G->>P: PartiallyAuthorizeActions (principal tags from claims)
        P-->>G: permitted actions
        G-->>A: alice: 7 tools, bob: 5 tools (layer 4)
        A->>A: binding text marks missing tools (layer 5)
        A->>G: tools/call orders___create_return
        G->>P: AuthorizeAction
        alt bob
            P-->>G: deny
            G-->>A: error (denied by policy)
        else alice
            P-->>G: allow
            G-->>A: result
        end
    end
```

Details that matter:

- **Users client vs M2M client.** `shopassist-<env>-users` is a public client (no secret,
  `ALLOW_USER_PASSWORD_AUTH`). Its access tokens carry `cognito:groups`. `shopassist-<env>-m2m`
  (client_credentials, secret, scope `shopassist/invoke`) is used only by the Gateway→support-MCP
  outbound OAuth and for local development against the real Gateway.
- **A runtime with a JWT authorizer doesn't accept SigV4.** That's why `invoke_agent.py` calls the
  HTTPS invocation URL directly with a Bearer token. (`--sigv4` exists for runtimes without a JWT
  authorizer.)
- **Header allowlist.** `requestHeaderConfiguration.requestHeaderAllowlist = ["Authorization"]` is what
  lets the agent see the token at all. Without it, the agent answers "no Authorization header reached
  the agent" (`error: missing_authorization`).
- **Passthrough plumbing.** `bind_request_token()` puts the token into a contextvar *and* a
  process-wide relay. The relay is needed because Strands' `MCPClient` runs on a background thread whose
  context was copied when the client started. The agent caches its tool list per token `sub` and
  rebuilds the MCP client and agent when the `sub` changes.
- **Cedar specifics.** The principal type is `AgentCore::OAuthUser`. JWT claims become string tags,
  and arrays arrive as JSON text, so the policies match with
  `principal.getTag("cognito:groups") like "*\"Group-ShopAssist-Admin\"*"`. Actions are Gateway tool names
  (`AgentCore::Action::"orders___get_order"`). The viewer policy lists allowed actions instead of
  forbidding the write ones, so a new tool is denied for viewers by default.
- **Templates.** `infra/policies/admin_all_tools.cedar`, `viewer_read_only.cedar` and
  `m2m_dev_client.cedar` have `{{GATEWAY_ARN}}` / `{{M2M_CLIENT_ID}}` placeholders, filled by
  Terraform (`replace()`). The file name becomes the policy name.
- **Ordering.** Policy engine → Gateway (with `policyEngineConfiguration`, mode `ENFORCE`) → targets →
  policies. `CreatePolicy` validates action names against the Gateway's tools, so the targets must
  exist first.
- **Enforcement doesn't depend on the agent.** `scripts/demo_access.py` (design.md §8) calls the Gateway
  directly with bob's token (`tools/call orders___create_return`) and expects a denial.

## Identity design

```mermaid
flowchart TB
    subgraph Cognito["Cognito shopassist-prod-pool"]
        UC["shopassist-prod-users<br/>public, USER_PASSWORD_AUTH"]
        MC["shopassist-prod-m2m<br/>client_credentials + secret"]
        RS["resource server shopassist<br/>scope shopassist/invoke"]
        GR["groups Group-ShopAssist-Admin, -Viewer<br/>users alice, bob, carol"]
    end
    UC -- "user access token" --> AR["Agent runtime<br/>allowed_clients = users"]
    AR -- "same token" --> GW["Gateway<br/>allowed_clients = users, m2m"]
    MC -- "client id + secret" --> CP["AgentCore Identity<br/>credential provider shopassist-prod-cognito<br/>(secret in Secrets Manager)"]
    GW -- "GetResourceOauth2Token" --> CP
    GW -- "M2M token" --> MR["Support MCP runtime<br/>allowed_clients = m2m"]
```

| Hop | Inbound check | Credential |
|---|---|---|
| CLI → agent runtime | JWT authorizer: users client + `cognito:groups` claim | User access token |
| Agent → Gateway | JWT authorizer: users or M2M client, then Cedar | Same user token (passthrough) |
| Gateway → `orders` Lambda | IAM | Gateway execution role (`lambda:InvokeFunction`) |
| Gateway → `support` MCP runtime | JWT authorizer: M2M client | client_credentials token from the AgentCore Identity provider `shopassist-<env>-cognito` (scope `shopassist/invoke`) |
| Local dev → real Gateway (optional) | JWT authorizer: M2M client, Cedar `m2m_dev_client` | `GATEWAY_AUTH_MODE=client_credentials` with `COGNITO_TOKEN_URL`, `COGNITO_CLIENT_ID`, `COGNITO_CLIENT_SECRET` |

The credential provider (vendor `CustomOauth2`, Cognito discovery URL) stores the M2M client secret
in an AgentCore-managed Secrets Manager secret. The Gateway role can call
`GetWorkloadAccessToken` / `GetResourceOauth2Token` and read that secret. The Cognito domain
`shopassist-<env>-<account>` must exist before the provider is created, because Cognito only advertises
`token_endpoint` in its discovery document once a domain is set.

The deployed agent role has no credential-provider (token vault) or Secrets Manager permissions:
in passthrough mode the agent never mints its own token. It only has the workload-identity token
APIs (`GetWorkloadAccessToken*`) that the Runtime uses with a JWT inbound authorizer. The runtime
still gets `OAUTH_PROVIDER_NAME` / `OAUTH_SCOPES` env vars, but the current agent code doesn't read
them.

## Data model

| Table | Key | Written by | Notes |
|---|---|---|---|
| `shopassist-<env>-orders` | `order_id` (S); GSI `customer_id-index` on `customer_id` (projection ALL) | `scripts/seed_data.py`; `create_return` sets `items[i].returned` | Order object as in [02](02-agent-capabilities.md#tool-catalogue) |
| `shopassist-<env>-returns` | `return_id` (S) | `create_return` | `return_id, order_id, customer_id, sku, reason, status, refund_amount, label_url, created_at` |
| `shopassist-<env>-tickets` | `ticket_id` (S) | `create_ticket` | `ticket_id, customer_id, order_id, subject, description, priority, status, created_at` |

All tables are `PAY_PER_REQUEST`. `data/seed.json` has 3 customers and 10 orders with relative dates.
`seed_data.py` resolves them to ISO dates and overwrites by key, so re-running it before a demo resets
the return windows. It doesn't touch returns or tickets. `mcp_servers/common/store.py` hides the
backend: `STORE=memory` (seeded in-process, used by the local emulator and tests) or DynamoDB.

## Packaging and deployment model

Every workload is an **arm64 container image** in ECR, tagged with the full git SHA, built and scanned only in
GitHub Actions and rolled out by Terraform (`-var image_tag=<sha>`):

| Image (ECR repo) | Dockerfile | Runs on | Entry point |
|---|---|---|---|
| `shopassist-agent` | `agent/Dockerfile` | AgentCore Runtime, arm64 | `python app.py` (HTTP on 8080: `/invocations`, `/ping`) |
| `shopassist-support-mcp` | `mcp_servers/support_mcp/Dockerfile` | AgentCore Runtime, arm64 | `python server.py` (stateless streamable HTTP on `0.0.0.0:8000/mcp`) |
| `shopassist-orders-lambda` | `mcp_servers/orders_lambda/Dockerfile` | Lambda, arm64 | `handler.lambda_handler` |
| `shopassist-role-mapper` | `identity/role_mapper/Dockerfile` | Lambda, arm64 (only with Entra) | `handler.lambda_handler` |
| `shopassist-chat-relay` | `chat_relay/Dockerfile` | Lambda, arm64 (the hosted chat page's backend) | `handler.lambda_handler` |

- Dependencies are pinned with hashes (`requirements.lock.txt`, generated from `uv.lock`), so two builds of one
  commit are identical.
- ECR tags are immutable and each repository keeps 2 images (current + previous, the rollback target).
- Skills are not in any image: they go to `s3://<bucket>/skills/` with each deploy, and a skill change only
  needs a new session.
- The full pipeline (workflows, approval, OIDC roles) is in [10-ci-cd.md](10-ci-cd.md); the Terraform stacks
  in [04-deploy-terraform.md](04-deploy-terraform.md).

## Observability

| Signal | Where |
|---|---|
| Agent logs (skills loaded, Gateway tools visible to the caller, blocked tool calls, errors) | CloudWatch `/aws/bedrock-agentcore/runtimes/<agent-runtime-id>-DEFAULT` |
| Support MCP server logs | `/aws/bedrock-agentcore/runtimes/<mcp-runtime-id>-DEFAULT` |
| Lambda tool calls (`tool=... args=[...]`, argument names only) | `/aws/lambda/shopassist-<env>-orders-tools` |
| Per-reply usage | `skills_used` / `tools_used` in every response, printed by the CLIs |
| Gateway target errors | Returned to the caller in detail because Terraform sets the Gateway `exception_level = "DEBUG"` (drop this in production) |
| Metrics, traces | Runtime roles may write X-Ray segments and CloudWatch metrics in the `bedrock-agentcore` namespace |

Terraform creates every log group with 7-day retention. The agent image doesn't include an
OpenTelemetry distro, so you get logs but no automatic agent spans in CloudWatch GenAI
observability. To get traces, add `aws-opentelemetry-distro` to `agent/requirements.in` (then re-pin, see [10](10-ci-cd.md#3-how-the-images-are-built)) and enable
CloudWatch Transaction Search in the account.

Locally, `scripts/run_local.py --verbose` turns on debug logging, including the binding decisions.

## Security notes and production hardening

What the PoC already does:

- Access is enforced outside the agent: Runtime JWT + claim rule, Gateway JWT, Cedar default deny with
  filtered `tools/list`.
- Business rules (eligibility) are enforced in the tool, and `create_return` re-checks.
- Trust policies for AgentCore roles use `aws:SourceAccount` / `aws:SourceArn` (confused-deputy
  guard). Each role only gets its own tables, its own image and the skills prefix; every role carries the
  environment's permissions boundary.
- The S3 bucket blocks public access. Secrets live in the AgentCore-managed secret, and
  `.deploy/` (users file, outputs, session ids) is git-ignored.
- Skills can't name endpoints or credentials. They only name servers the Gateway already exposes,
  and the binding blocks undeclared servers.
- `read_skill_resource` is path-traversal safe.

What to change before production:

| Area | PoC choice | Production |
|---|---|---|
| Customer identity | The customer id is whatever the user types; the agent doesn't bind it to the caller | Derive the customer from the token (claim) and enforce it in the tools or Cedar (e.g. a `customer_id` tag condition) |
| User sign-in | Demo users: `USER_PASSWORD_AUTH`, passwords in `.deploy/<env>/users.json` (and in Terraform state). Entra users: authorization code + PKCE (optional) | Hosted UI / authorization code + PKCE, MFA, no passwords on disk |
| Prompt injection | Tool output and FAQ text are trusted | Bedrock Guardrails, output validation, treat tool text as data |
| Gateway errors | `exception_level = DEBUG` (Terraform) | Default (no details to callers) |
| Network | `PUBLIC` network mode | VPC mode, VPC endpoints, private DynamoDB access |
| Delivery | Done: remote state (S3 + lock file), GitHub Actions with OIDC, approved manual deploys | Add a second account for dev/staging, signed images, drift detection |
| Tokens | 60-minute Cognito access tokens; `invoke_agent.py` signs in again on every run | Short-lived tokens, refresh, revocation |
| Policy rollout | Straight to `ENFORCE` | Try changes in `LOG_ONLY` first, then switch to `ENFORCE` |
| Data | Fake labels, no PII protection | Encryption with CMKs, PITR, retention rules, PII redaction in logs |
| Observability | Logs only | OTEL traces, dashboards, alarms on errors and policy denials |
| Least privilege | `dynamodb:Scan` is granted though unused | Trim actions to what the code calls |
| Conversation memory | In-process only, per microVM; lost on idle timeout or redeploy; 40-message window | AgentCore Memory or a Strands session manager (S3/DynamoDB); set explicit Runtime lifecycle timeouts |
