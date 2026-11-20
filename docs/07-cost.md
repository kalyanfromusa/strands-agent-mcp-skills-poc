# 7. Cost

ShopAssist only uses on-demand services. An idle stack costs close to nothing, and a demo costs
roughly what its Bedrock tokens cost.

> **No prices on this page are authoritative.** Prices change and vary by region. Look up the
> current numbers on the official pages before you rely on an estimate:
> - Amazon Bedrock: https://aws.amazon.com/bedrock/pricing/
> - Amazon Bedrock AgentCore (Runtime, Gateway, Policy, Identity, ...): https://aws.amazon.com/bedrock/agentcore/pricing/
> - Lambda, DynamoDB, Cognito, S3, Secrets Manager, CloudWatch: their pricing pages on aws.amazon.com
>
> Every figure marked *illustrative* is there to show how to calculate, not what you'll pay.

## What drives cost

| Service | What's billed | Driver in this PoC | Idle cost |
|---|---|---|---|
| Bedrock (Claude Haiku 4.5, `us.anthropic.claude-haiku-4-5-20251001-v1:0`) | Input and output tokens | Every model call in the agent loop | None |
| AgentCore Runtime (agent + support MCP server) | Consumption: vCPU-seconds and GB-seconds of memory per session, billed per second | Active sessions. The agent spends most of its time waiting on Bedrock and tools; check the pricing page for how CPU during I/O wait and session idle time are counted | None (no provisioned capacity) |
| AgentCore Gateway | Per API request (`tools/list`, `tools/call`), plus tool indexing (per tools indexed per month) | ~5–15 requests per conversation; 7 tools indexed | Tool indexing for 7 tools (tiny) |
| AgentCore Policy | Per authorization request | Every `tools/list` and `tools/call` on the Gateway | None |
| AgentCore Identity | Token requests through the credential provider | Gateway → `support` target tokens. Check the pricing page for whether usage through Runtime/Gateway is charged | None |
| Secrets Manager | Per secret-month (+ API calls) | One AgentCore-managed secret for the credential provider (holds the M2M client secret) | About one secret-month, prorated |
| Cognito | MAUs; M2M (client_credentials) token requests are priced separately from MAUs | 3 demo users; Gateway M2M tokens for `support` (cached until expiry) | None |
| Lambda (`shopassist-orders-tools`, arm64, 256 MB) | Requests + GB-seconds | ~2–5 invocations per conversation | None |
| DynamoDB on-demand (3 tables) | Read/write request units + storage | A few reads/writes per conversation, kilobytes of data | Storage (KB) |
| S3 (artifacts bucket: skills only; the Terraform state bucket) | Storage + requests | A few KB of skills and state files | Negligible |
| ECR (4 repositories, 2 images each) | Storage per GB-month | About 8 images of a few hundred MB | Image storage (cents per month) |
| GitHub Actions | Free for public repositories (standard and arm64 runners) | Every PR, merge and deploy | None |
| CloudWatch Logs (7-day retention) | Ingestion + storage | Runtime and Lambda logs | Storage, 7 days max |
| AWS Budgets (optional: `enable_budget` in `envs/<env>.tfvars`) | Budgets themselves are generally free; check the pricing page | 1 budget | n/a |

## Bedrock tokens per conversation (estimate)

The agent loop sends the whole context on every model call. Approximate sizes for this code base:

| Part of each request | ≈ tokens |
|---|---|
| Generic system prompt + `<available_skills>` (3 names + descriptions) | 500 |
| Tool specs: 7 Gateway tools (the `orders` descriptions are long) + `skills` + `read_skill_resource` | 1,500 |
| Activated skill instructions + `[Tool binding]` block (after activation) | 800–1,000 |
| Tool results (order JSON, eligibility, return) | 100–300 each |

A typical "return an item" turn makes about 5 model calls (activate skill → `get_order` →
`check_return_eligibility` → `create_return` → final answer), with context growing from ~2k to ~4k
tokens. That's **~15k input and ~500 output tokens per turn**. A demo conversation of about 3 turns
lands around **40k input and 2k output tokens**. These are estimates from the prompt and schema sizes,
not measurements. To measure, use the CloudWatch metrics for Bedrock invocations, or Strands'
`result.metrics` locally.

*Illustrative* calculation for **100 demo conversations** (4M input, 0.2M output tokens), using
placeholder prices only, **not** quoted Haiku prices:

| If input costs ... per 1M tokens | and output ... per 1M | 100 conversations ≈ |
|---|---|---|
| $0.25 | $1.25 | $1.25 |
| $1.00 | $5.00 | $5.00 |

Plug in the current prices for your model and region from the Bedrock pricing page.

## Order-of-magnitude monthly estimate: 100 demo conversations

*Illustrative*, for one region with the stack deployed all month:

| Item | Order of magnitude |
|---|---|
| Bedrock tokens | A few dollars (see above; this dominates) |
| AgentCore Runtime (2 runtimes, ~100 short sessions each) | Cents to low single-digit dollars, depending on session length and idle timeout |
| AgentCore Gateway + Policy (~1,000–1,500 requests each) | Cents |
| AgentCore Identity | Zero to cents |
| Secrets Manager (1 secret) | Under a dollar |
| Cognito, Lambda, DynamoDB, S3 | Zero to cents (often inside free tier) |
| CloudWatch Logs (7 days) | Cents |
| **Total** | **Roughly single-digit dollars per month** |

If you deploy for a day and destroy, the fixed parts (secret, S3, logs) are prorated to almost nothing.

What would change this:

- **Longer conversations.** Input tokens grow with turns × calls per turn, because the full history
  is re-sent.
- **A bigger model.** Switching `model_id` to a larger model scales the token line up.
- **Long-lived sessions.** Runtime sessions stay alive until their idle timeout. Many parallel
  sessions held open increase Runtime consumption.
- **Semantic tool search.** It's off (no `searchType`). Enabling it adds search requests.

Ways to reduce token cost (not implemented): Bedrock prompt caching for the system prompt and tool
specs, shorter tool descriptions in `tools_schema.json`, and fewer tools per skill.

## Cost controls in this repo

| Control | How |
|---|---|
| 7-day log retention | `log_retention_days = 7` in `envs/<env>.tfvars`. Log groups are created up front (or, for the ones AgentCore creates itself, given a retention by `scripts/log_group.py`) so they never default to "never expire" |
| On-demand everything | DynamoDB `PAY_PER_REQUEST`, Lambda, Runtime consumption billing, no NAT gateways or VPC endpoints (network mode `PUBLIC`) |
| Budget alert (Terraform) | Set `enable_budget = true`, `budget_email`, `budget_limit_usd` (default 5) in `envs/<env>.tfvars`. It alerts at 80% actual and 100% forecasted. The budget is account-wide, because tag-filtered budgets need cost-allocation tags activated in Billing first |
| Tag everything | `project=shopassist` on every taggable resource (and `environment=<env>`, through Terraform `default_tags`), so leftovers are easy to find |
| Create and destroy on demand | `destroy.yml` removes a whole environment and scans for leftovers; `deploy.yml` recreates it. `scripts/destroy_everything.sh` also removes the (free) foundation ([04 §7](04-deploy-terraform.md#7-destroy)) |
| ECR keeps 2 images | Lifecycle rule per repository (current + previous); only deploys push |
