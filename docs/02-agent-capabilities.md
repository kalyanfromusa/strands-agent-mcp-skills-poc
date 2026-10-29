# 2. Agent capabilities

The agent itself knows nothing about the store. Everything below comes from the three skills in
`skills/` and the seven MCP tools behind the Gateway.

## Skills

Each skill is a folder with a `SKILL.md` (YAML frontmatter + instructions). The frontmatter
`description` is what the model sees before activation, and `metadata.mcp-servers` declares the
Gateway targets the skill may use.

| Skill | Activates when the user... | `mcp-servers` | Tools it uses |
|---|---|---|---|
| `order-tracking` | asks where an order is, whether it shipped or was delivered, for tracking/carrier, what was in an order, or for a list of past orders | `orders` | `list_customer_orders`, `get_order` |
| `returns-and-refunds` | wants to return an item, asks whether something is returnable, about the return window or policy, or about a refund | `orders support` | `list_customer_orders`, `get_order`, `check_return_eligibility`, `create_return`, `create_ticket` (for disputes) |
| `support-ticketing` | has a general question (shipping, payment, account, policies), reports a problem that needs a person, or wants to open or check a ticket | `support` | `search_faq`, `get_ticket`, `create_ticket` |

What each skill does:

- **order-tracking.** Identifies the order (order id, or customer id then `list_customer_orders`),
  calls `get_order`, and answers by status: `PROCESSING` gives the order date, `SHIPPED` the carrier and
  tracking number, `DELIVERED` the delivery date, and `CANCELLED` says it was cancelled without
  speculating why. It hands off to returns or ticketing for follow-ups.
- **returns-and-refunds.** Collects the order and item, finds the SKU with `get_order`, always calls
  `check_return_eligibility`, explains a refusal in friendly words (and offers a ticket for special
  cases), confirms, then calls `create_return` and gives the return id, refund amount and label link.
  The plain-language policy is in `references/return-policy.md`, which the agent reads with
  `read_skill_resource` when the customer asks about the rules.
- **support-ticketing.** Answers how/what/when questions from `search_faq`, looks up tickets with
  `get_ticket`, and opens tickets with `create_ticket`. It picks the priority (`high` for money taken
  but nothing received, safety issues or a missing order, `low` for feedback, `medium` otherwise).

Requests that match no skill (for example "write me a poem") get a short answer listing what the
agent can help with. This comes from the generic system prompt in `agent/app.py`.

## Tool catalogue

The Gateway names every tool `<target>___<tool>` (three underscores). Skills use the plain name, and
the agent resolves it (see [03-architecture.md](03-architecture.md#generic-agent-design)).

| Gateway name | Target | Input | Output (JSON as text) |
|---|---|---|---|
| `orders___get_order` | `orders` (Lambda) | `order_id: str` | Order object, or `{"error": "ORDER_NOT_FOUND", "order_id"}` |
| `orders___list_customer_orders` | `orders` (Lambda) | `customer_id: str` | `{"customer_id", "orders": [{order_id, status, order_date, total}]}`, newest first |
| `orders___check_return_eligibility` | `orders` (Lambda) | `order_id: str`, `sku: str` | `{"eligible", "reason", "days_since_delivery", "return_window_days", "refund_amount"}` |
| `orders___create_return` | `orders` (Lambda) | `order_id: str`, `sku: str`, `reason: str` | `{"return_id": "RET-xxxxxx", "status": "REQUESTED", "refund_amount", "label_url"}`, or `{"error": "NOT_ELIGIBLE", "reason"}` |
| `support___create_ticket` | `support` (MCP server on Runtime) | `customer_id`, `subject`, `description`, `order_id = ""`, `priority = "medium"` (`low`/`medium`/`high`) | `{"ticket_id": "TCK-xxxxxx", "status": "OPEN", "created_at"}` |
| `support___get_ticket` | `support` | `ticket_id: str` | Ticket object, or `{"error": "TICKET_NOT_FOUND", "ticket_id"}` |
| `support___search_faq` | `support` | `query: str` | `{"results": [{"question", "answer"}]}`: up to 3 keyword matches from the bundled `faq.json` (8 entries) |

The order object is `order_id, customer_id, status (PROCESSING|SHIPPED|DELIVERED|CANCELLED),
order_date, delivered_date (or null), carrier, tracking_number, items: [{sku, name, category, qty,
price, returned}], total`.

The Lambda also returns `{"error": "UNKNOWN_TOOL"}` or `{"error": "INVALID_INPUT", "message"}` for
calls it can't route or bind. The `orders` schemas the Gateway advertises are in
`mcp_servers/orders_lambda/tools_schema.json`. The `support` schemas come from the FastMCP server's
own `tools/list` (function signatures in `mcp_servers/support_mcp/server.py`).

Where the code lives:

| Code | Path |
|---|---|
| Orders tool functions and Lambda router | `mcp_servers/orders_lambda/handler.py` |
| Return rules (pure functions) | `mcp_servers/common/rules.py` |
| Data access (memory or DynamoDB) | `mcp_servers/common/store.py` |
| Support MCP server | `mcp_servers/support_mcp/server.py`, `faq.json` |

## Return policy: enforced by the tool, explained by the skill

`mcp_servers/common/rules.py::check_eligibility` applies these rules, in this order:

| # | Rule | Result if it fails |
|---|---|---|
| 1 | The order exists | Not eligible: "Order not found..." |
| 2 | The SKU is part of the order (case-insensitive) | Not eligible: "Item ... is not part of order ..." |
| 3 | The order is `DELIVERED` and has a `delivered_date` | Not eligible: "Order ... is SHIPPED, not DELIVERED..." |
| 4 | The item hasn't been returned already | Not eligible: "... has already been returned." |
| 5 | Category isn't `final-sale` | Not eligible: "... is a final-sale item..." |
| 6 | Days since delivery ≤ window: `electronics` 15 days, everything else 30 (inclusive, UTC) | Not eligible: "Delivered N days ago, outside the W-day return window..." |
| ✓ | All passed | Eligible, `refund_amount = price × qty` |

`create_return` **re-runs the same check server-side** before writing anything, so a model that
skips the eligibility step still can't create an ineligible return. On success it stores the return,
marks the item `returned: true` (so a second return is refused) and returns a fake label URL
`https://returns.shopassist.example.com/labels/<return_id>.pdf`.

The skill's `references/return-policy.md` describes the same rules in customer language. The two can
drift apart. If they do, the tool wins, because the agent is told to explain the tool's `reason`
rather than apply the policy itself.

## Guardrails

Guardrails come from three places:

| Guardrail | Where |
|---|---|
| Never invent data: every fact, id, amount or status must come from a tool result in this conversation | System prompt (`agent/app.py`) and every skill's "Never" section |
| Ask for missing details instead of guessing ids | System prompt, skill steps |
| Activate a skill before using any other tool | System prompt, and enforced by the `SkillBinding` hook: a Gateway tool call is cancelled unless an activated skill declares its server |
| Eligibility before return | Skill: "Never call `create_return` before `check_return_eligibility` has said the item is eligible". Tool: `create_return` re-checks |
| No duplicate returns or tickets | Skill instructions, and the tool refuses a second return of the same item |
| Don't promise refunds, timelines or exceptions the tools didn't give | Skill instructions |
| Don't work around a missing tool | Skill instructions, and the binding text "Not available to the current user: ..." |
| Don't mention internal tool names or skills to the user | System prompt |
| Read only files inside an activated skill | `read_skill_resource` rejects absolute paths, `..` traversal, inactive skills, and files over 200 KB |

## Behaviour per user role

| Caller | Tools the agent sees | Typical behaviour |
|---|---|---|
| `alice` (`shop-admin`) | All 7 | Full flows: lookups, returns, tickets |
| `bob` (`shop-viewer`) | 5 read-only (no `create_return`, no `create_ticket`) | Looks up orders, checks eligibility, quotes the refund amount, searches the FAQ. When asked to create a return or ticket, says the account lacks permission and offers what it can do |
| `carol` (no group) | None | HTTP 403 from the Runtime. The agent never runs |
| Local emulator `--role admin` | All 7 | Like alice |
| Local emulator `--role viewer` | 5 read-only | Like bob (the emulator filters `tools/list` and rejects other calls) |

How bob's experience works: the agent lists Gateway tools with bob's own token, and AgentCore
Policy filters `tools/list`, so `orders___create_return` is simply not registered. When
`returns-and-refunds` is activated, the binding text ends with:

```
Not available to the current user: create_ticket, create_return
If a step needs an unavailable tool, tell the user their account doesn't have permission for that
action and offer what you can do instead. Do not try to work around it.
```

Even if the model tried anyway, the Gateway would deny the call. `scripts/demo_access.py` checks
this by calling `tools/call orders___create_return` directly with bob's token.
