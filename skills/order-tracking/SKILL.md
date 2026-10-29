---
name: order-tracking
description: Use when a customer asks where their order is, whether it has shipped or been delivered, for tracking or carrier details, what was in an order, or wants a list of their past orders. Not for returns, refunds or complaints.
metadata:
  mcp-servers: "orders"
---
# Order tracking

Help the customer find out the status of an order and what's in it. All order facts come from tools.

## Steps

1. **Identify the order.**
   - If the customer gives an order id, go to step 2.
   - If they don't know it, ask for their customer id, then call `list_customer_orders` and show the orders briefly (order id, date, status, total). Ask which one they mean if it isn't obvious.
   - If they have neither, ask for one of them. Don't guess ids.
2. **Look it up.** Call `get_order` with the order id.
   - If the result is an error such as not found, tell the customer you couldn't find that order and ask them to double-check the id. Don't retry with made-up variations.
3. **Answer by status.**
   - `PROCESSING`: it hasn't shipped yet; give the order date.
   - `SHIPPED`: give the carrier and tracking number exactly as returned.
   - `DELIVERED`: give the delivery date.
   - `CANCELLED`: say it was cancelled. Don't speculate why.
   Mention the items only if the customer asked or it helps (name and quantity).
4. **Follow-ups.**
   - If they want to return or exchange something, use the returns-and-refunds skill.
   - If something is wrong (missing parcel, damaged item, delivered but not received), offer to open a support ticket with the support-ticketing skill.

## Never

- Never invent or estimate a delivery date, tracking number or carrier. If a field is empty, say it isn't available yet.
- Never show another customer's orders. Only look up what this customer gave you.
- If a tool you need isn't available to you, tell the customer their account doesn't have permission for that action and offer what you can do instead. Never try to work around a missing tool.

## Response style

Short and friendly: one or two sentences with the key facts, then a bullet list only if there are several items or orders. Write dates in a readable form (e.g. 3 March 2026).
