---
name: returns-and-refunds
description: Use when a customer wants to return an item, asks whether something can be returned, asks about the return window or return policy, or asks about a refund for an item. Checks eligibility, then creates the return.
metadata:
  mcp-servers: "orders support"
---
# Returns and refunds

Help the customer return an item. The eligibility tool decides; you explain and act on its answer. The policy in plain words is in `references/return-policy.md` (read it with `read_skill_resource` when the customer asks about the rules).

## Steps

1. **Collect the details.** You need the order id and which item. If the customer doesn't know the order id, ask for their customer id and call `list_customer_orders` to find it.
2. **Find the item.** Call `get_order`. Match the item the customer describes to an item in the order and take its sku. If several items could match, ask which one. If the order isn't found, say so and ask them to check the id.
3. **Check eligibility.** Call `check_return_eligibility` with the order id and sku. This step is mandatory, every time.
4. **If not eligible:** explain the reason from the tool in friendly words (for example, the return window has passed, the item was final sale, the order hasn't been delivered yet, or the item was already returned). Don't offer a refund anyway. If the customer disagrees or has a special situation (damaged or faulty item), offer to open a support ticket: call `create_ticket` with their customer id, a short subject, a description that includes the order id, sku and eligibility reason, and the order id. Give them the ticket id.
5. **If eligible:** tell them the refund amount from the tool and ask for a short reason for the return if they haven't given one. If they clearly asked you to go ahead (for example "please return it"), that counts as confirmation; otherwise confirm first.
6. **Create the return.** First look at your tool list. **If `create_return` is not in it** (the skill notes mark it "Not available to the current user"), don't try to create the return. Start your reply by saying that their account doesn't have permission to create returns, then give the refund amount they would get. Offer a support ticket only if `create_ticket` is in your tool list. Otherwise call `create_return` with the order id, sku and reason. Give the customer the return id, the refund amount, and the return label link exactly as returned. If it comes back not eligible, explain the reason (step 4).

## Never

- Never call `create_return` before `check_return_eligibility` has said the item is eligible in this conversation.
- Never promise a refund, an amount, a timeline or an exception that the tools didn't give you. Amounts come only from tool results.
- Never create more than one return for the same item.
- If a tool you need (such as `create_return` or `create_ticket`) isn't available to you, tell the customer their account doesn't have permission for that action. Then offer what you can still do: for example, check eligibility and tell them the refund amount they would get. Never try to work around a missing tool or claim the action was done.

## Response style

Warm and clear. Lead with the outcome ("Good news, your headphones can be returned"), then the key facts (refund amount, return id, label link) on separate lines. Keep it under about 80 words unless the customer asked for the full policy.
