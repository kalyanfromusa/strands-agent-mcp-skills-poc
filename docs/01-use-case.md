# 1. Use case: ShopAssist

## The scenario

ShopAssist is the customer-support assistant of a fictional online store. Customers come to it with
three kinds of request:

| Request | Example | What the assistant needs |
|---|---|---|
| Order status | "Where is order 1042?" | Order lookup, carrier and tracking number |
| Returns and refunds | "I want to return the headphones from 1042." | The return policy, an eligibility check, a way to create the return |
| Support | "My parcel arrived damaged." / "How long does shipping take?" | FAQ search, opening and looking up support tickets |

The store's data (orders, returns, tickets) and its rules (return windows, final sale) live behind
tools. The assistant itself is a **generic agent**: it has no idea what a "return window" is until a
skill tells it how to handle returns and a tool tells it whether a given item is eligible.

## Personas

### The customer (conversation side)

The customer talks to the agent in plain language and identifies orders by order id (`1042`) or
customer id (`C001`). The demo data has three customers:

| Customer | Name | Orders worth trying |
|---|---|---|
| `C001` | Alice Martin | `1042` (headphones, delivered 5 days ago, returnable), `1001` (clothing, in window), `1002` (out of window), `1007` (one item already returned) |
| `C002` | Ben Okafor | `1003` (electronics at 10 days, returnable), `1004` (smartwatch at 20 days, not returnable; T-shirt returnable), `1008` (processing) |
| `C003` | Chloe Dubois | `1005` (final-sale swimsuit), `1006` (shipped, not delivered), `1009` (cancelled) |

Dates in `data/seed.json` are relative (`delivered_days_ago`), so these cases hold whenever you seed
or start the local emulator.

### The operators (access-control side)

The *caller* of the agent is a Cognito user. Think of them as support staff with different
permissions using the same assistant. The deployed demo creates three of them:

| User | Cognito group | Can use the agent? | Tools |
|---|---|---|---|
| `alice` | `shop-admin` | Yes | All 7 |
| `bob` | `shop-viewer` | Yes | Read-only: `get_order`, `list_customer_orders`, `check_return_eligibility`, `get_ticket`, `search_faq` |
| `carol` | none | No: HTTP 403 at the Runtime, before any agent code runs | None |

(Customer `C001` "Alice Martin" and operator `alice` are unrelated. The operator names follow the
usual alice/bob/carol convention.)

These permissions are enforced by AgentCore (the Runtime JWT authorizer and AgentCore Policy at the
Gateway), not by the agent code. See [03-architecture.md](03-architecture.md#per-user-access-control).

## Example conversations

These are the flows exercised in the local end-to-end run with Bedrock
(`us.anthropic.claude-haiku-5-5`). Replies are paraphrased. The dim line under each reply in the CLI
lists the skills and tools used.

**Eligible return (admin)**

> **User:** I'd like to return the headphones from order 1042, they're not comfortable. Please go ahead.
> **Agent:** Good news, your headphones can be returned. Return id: RET-…, refund: $199.99, label: https://returns.shopassist.example.com/labels/RET-….pdf
> `skills: returns-and-refunds | tools: orders___get_order, orders___check_return_eligibility, orders___create_return`

**Refused return, ticket offered**

> **User:** Can I return the smartwatch from order 1004?
> **Agent:** Unfortunately not: it was delivered 20 days ago, outside the 15-day window for electronics. If there's something wrong with it, I can open a support ticket so the team can take a look.

**Ticket**

> **User:** My order 1006 says shipped but it's been ages. My customer id is C003.
> **Agent:** I've opened a ticket for you, the team will follow up. TCK-…

**Off-topic**

> **User:** Can you write me a poem about the sea?
> **Agent:** I can help with order status, returns and refunds, and support questions or tickets. What can I do for you?

**Read-only user (`bob`, or the local emulator with `--role viewer`)**

> **User:** Please return the headphones from order 1042.
> **Agent:** They are eligible for a $199.99 refund, but your account doesn't have permission to create returns. I can check other orders or eligibility for you.
> `tools: orders___get_order, orders___check_return_eligibility` (no `create_return`: it isn't in this user's tool list at all)

**No access (`carol`, deployed only)**

`scripts/invoke_agent.py --user carol` prints `Access denied (HTTP 403)`. The request never reaches
the agent.

## Why this is a good PoC

- **Small, familiar domain.** Everyone understands orders and returns, so attention stays on the
  architecture. The rules are simple but real enough to show that an LLM shouldn't be the one
  enforcing them (return windows, final sale, "check before you act").
- **Shows the generic-agent pattern.** The agent code has no domain logic. Behaviour comes from
  three `SKILL.md` folders read at runtime from S3, and data and rules come from MCP tools. Adding a
  skill changes behaviour without a code change ([08-extending-with-skills.md](08-extending-with-skills.md)).
- **Covers the main AgentCore services.** Runtime (agent over HTTP, and an MCP server), Gateway
  (Lambda target and MCP-server target behind one MCP endpoint), Identity (OAuth2 credential
  provider) and Policy (Cedar, per-user tool filtering).
- **Shows per-user access outside the agent.** One agent, one Gateway, three users, three outcomes.
  A direct Gateway call with bob's token to `create_return` is denied, which shows the enforcement doesn't
  depend on the model behaving.
- **Cheap.** Everything is serverless and on demand (Runtime, Lambda, DynamoDB on-demand, S3), logs
  keep 7 days, and the stack is torn down with one command. See [07-cost.md](07-cost.md).
- **Two delivery paths.** The same resource graph in Terraform and in plain boto3 calls, so you
  can compare them ([06-terraform-vs-boto3.md](06-terraform-vs-boto3.md)).
