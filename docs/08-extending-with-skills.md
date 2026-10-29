# 8. Extending with skills and tools

The agent's behaviour lives in `skills/*/SKILL.md`. Adding a skill that uses existing tools needs
**no code change and no new agent zip**: write the folder, upload it, start a new session.

## The rules

1. **A skill never contains endpoints, URLs, credentials or `<server>___<tool>` names.** It declares
   servers in `metadata.mcp-servers` and uses plain tool names in backticks. The Gateway decides
   what exists and who may call it. The agent resolves names at activation time.
2. **A skill describes, the tool enforces.** Rules that matter (eligibility, limits) belong in the
   tool. The skill explains them and tells the model what to do with the result.
3. **The folder name equals `name`** (lowercase letters, digits and hyphens). The Strands loader warns
   if they differ.
4. **`description` is the trigger.** It's the only part the model sees before activation, so say
   when to use the skill and when not to.
5. **Plan for missing permissions.** Say what to do if a tool isn't available to the current user.

## Walkthrough: add an `order-changes` skill (existing tools only)

Goal: handle "can I change my delivery address / cancel my order?" Today that falls between
order-tracking and support-ticketing. The new skill uses `get_order` (server `orders`) and
`search_faq` / `create_ticket` (server `support`), all of which already exist.

### 1. Write the skill

Create `skills/order-changes/SKILL.md`:

```markdown
---
name: order-changes
description: Use when a customer wants to change the delivery address of an order or cancel an order. Checks the order status first, then hands the change request to the support team. Not for returns of delivered items.
metadata:
  mcp-servers: "orders support"
---
# Order changes and cancellations

Orders can only be changed or cancelled before they ship. A person on the support team makes the
change, so your job is to check the status and open a ticket.

## Steps

1. **Identify the order.** Ask for the order id if you don't have it. Call `get_order`.
   If it isn't found, say so and ask them to check the id.
2. **Check the status.**
   - `PROCESSING`: the change is still possible. Go to step 3.
   - `SHIPPED`: it can't be changed any more. Say so, give the carrier and tracking number, and
     suggest a return once it is delivered.
   - `DELIVERED`: suggest a return instead (the returns-and-refunds skill).
   - `CANCELLED`: say it is already cancelled.
   If the customer asks about the rules, call `search_faq` with "change cancel order" and answer
   from the result.
3. **Open a ticket.** You need the customer id and exactly what should change (the new address, or
   "cancel"). Call `create_ticket` once with subject "Order change request" or "Cancellation
   request", a description with the order id and the requested change, the order id, and priority
   `high`. Give the customer the ticket id and say the team will confirm.

## Never

- Never say the change or cancellation is done. A person still has to do it.
- Never invent order details. They come from `get_order` only.
- If `create_ticket` isn't available to you, tell the customer their account doesn't have permission
  to request changes, and tell them what you found about the order.

## Response style

Two or three short sentences. End with the ticket id on its own line when you created one.
```

Check that the plain names you used exist on the declared servers:

| Name in the skill | Gateway tool | Server declared? |
|---|---|---|
| `get_order` | `orders___get_order` | `orders` ✓ |
| `search_faq` | `support___search_faq` | `support` ✓ |
| `create_ticket` | `support___create_ticket` | `support` ✓ |

If you mention a tool that isn't on a declared server, the agent logs
`skill 'order-changes': tool `xyz` is not on its declared servers` at startup.

### 2. Try it locally first

```bash
uv run python scripts/local_gateway.py --port 8765                 # terminal 1
uv run python scripts/run_local.py --gateway-url http://localhost:8765/mcp \
  --prompt "Can I change the delivery address of order 1008? I'm C002, new address 5 Rue Neuve, Lyon."
```

`run_local.py` reads `./skills` at start, so restart it after editing a skill. Expect
`skills: order-changes | tools: orders___get_order, support___create_ticket` in the dim line
(order 1008 is `PROCESSING`). Try `1006` (`SHIPPED`) to see the refusal path.

### 3. Upload it to S3

The deployed agent reads skills from `SKILLS_S3_URI` (`s3://shopassist-artifacts-<account>-<region>/skills/`).
Upload with the same tool you deployed with, so that its state stays accurate.

**Terraform** (the `aws_s3_object.skill` resources mirror `skills/**`):

```bash
cd infra/terraform
terraform apply        # plan shows only the new skill object(s)
```

**boto3** (the S3 step mirrors `skills/`, including deletions):

```bash
uv run python infra/boto3/deploy.py      # re-running is idempotent; unchanged resources are skipped
```

**Direct S3 sync** (quick, but a Terraform-managed bucket will show drift on the next plan):

```bash
SKILLS_URI=$(uv run python -c "import json; print(json.load(open('.deploy/outputs.json'))['skills_s3_uri'])")
aws s3 sync skills/ "$SKILLS_URI" --delete --exclude "*/__pycache__/*"
```

### 4. Start a new session

The agent syncs skills from S3 **once per microVM**, at the first invocation of a session. Existing
sessions keep the skill set they started with until they end. Start a new session:

```bash
uv run python scripts/invoke_agent.py --user alice --new-session \
  --prompt "Please cancel order 1008, I'm customer C002."
```

(In interactive mode, type `/new`.)

### 5. Verify

- The dim line shows `skills: order-changes`.
- The agent runtime log (`/aws/bedrock-agentcore/runtimes/<agent-runtime-id>-DEFAULT`) shows
  `loaded 4 skills from /tmp/skills: [...]` for the new session.
- As `bob`: the binding marks `create_ticket` as not available, and the agent reports the order status
  and explains that his account can't request changes.
- Nothing else changed: `git status` shows only `skills/order-changes/`.

To remove a skill, delete the folder and upload again. Terraform and the boto3 step both delete
objects that no longer exist locally, and so does the `aws s3 sync --delete` command.

### Optional: reference files

Put longer material under `skills/<name>/references/` and point to it from the instructions
("read `references/address-rules.md` with `read_skill_resource`"). The activation result lists the
files. The model reads them only when needed (progressive disclosure), and `read_skill_resource`
only serves files of activated skills.

## Adding a new tool

A new capability needs a tool. Tools are code, so this *does* need a build and deploy.

### Lambda tool on the `orders` target

Example: `get_return(return_id)` to look up a return.

1. **Implement it** in `mcp_servers/orders_lambda/handler.py` and register it in `TOOLS`. Add a
   `get_return` method to the stores in `mcp_servers/common/store.py` (`MemoryStore` and `DynamoStore`).
   The Lambda role can already `GetItem` on the returns table.

   ```python
   def get_return(return_id: str) -> dict[str, Any]:
       """One return by id, or RETURN_NOT_FOUND."""
       record = get_store().get_return(str(return_id).strip().upper())
       return record if record else {"error": "RETURN_NOT_FOUND", "return_id": return_id}

   TOOLS["get_return"] = get_return
   ```

2. **Describe it** in `mcp_servers/orders_lambda/tools_schema.json`. A Lambda has no `tools/list`, so
   the Gateway only knows what this file says:

   ```json
   {
     "name": "get_return",
     "description": "Look up a return by its id (RET-xxxxxx): status, refund_amount, label_url, order_id and sku. Returns {\"error\": \"RETURN_NOT_FOUND\"} if it does not exist.",
     "inputSchema": {
       "type": "object",
       "properties": {"return_id": {"type": "string", "description": "Return id, e.g. \"RET-1A2B3C\"."}},
       "required": ["return_id"]
     }
   }
   ```

   `tests/test_orders_lambda.py::test_schema_matches_tools` fails if `TOOLS` and the schema disagree.

3. **Decide who may call it** in `infra/policies/`:
   - `admin_all_tools.cedar` and `m2m_dev_client.cedar` permit every action, so alice gets it
     automatically.
   - `viewer_read_only.cedar` is an allow-list, so bob **doesn't** get it until you add
     `AgentCore::Action::"orders___get_return"`. Read-only tools usually belong there.
   - Mirror the change in `VIEWER_TOOLS` in `scripts/local_gateway.py` so `--role viewer` behaves the
     same locally.

4. **Test, build and deploy:**

   ```bash
   uv run pytest
   uv run python scripts/build.py --only orders_lambda
   cd infra/terraform && terraform apply          # or: uv run python infra/boto3/deploy.py
   ```

   Both paths update the Lambda code (new S3 object version), the target's inline schema and the
   policies, in that order. `CreatePolicy`/`UpdatePolicy` validates action names against the
   Gateway's tools, so the target must have the tool before a policy can name it (see
   [09](09-troubleshooting.md#policy-create_failed)).

5. **Use it from a skill.** Mention `get_return` in a skill that declares `orders`, upload the skill and
   start a new session. Agent sessions build their tool list when they start, so the new tool also
   needs a new session.

### MCP tool on the `support` target

Add a function to `mcp_servers/support_mcp/server.py` and to `SUPPORT_TOOLS`. The schema comes from
the function signature and docstring, so there's no separate schema file. Rebuild
(`scripts/build.py --only support_mcp`) and deploy. The Gateway caches an MCP-server target's tool list:

- The boto3 deploy calls `SynchronizeGatewayTargets` when the MCP runtime changed.
- With Terraform the target resource itself doesn't change, so re-sync by hand after `apply`:

  ```bash
  aws bedrock-agentcore-control synchronize-gateway-targets \
    --gateway-identifier <gateway_id> --target-id-list <support target id>
  ```

  `gateway_id` is in `.deploy/outputs.json`. Get the target id with
  `aws bedrock-agentcore-control list-gateway-targets --gateway-identifier <gateway_id>`.

Then update the Cedar viewer allow-list and `scripts/local_gateway.py` as for a Lambda tool.

### A new target (new server)

A new target name becomes a new prefix (`<target>___*`) and a new value for `mcp-servers`. Add it in
both IaC paths (Terraform `modules/gateway`, boto3 `steps/gateway.py`), in the local emulator, and in
the Cedar policies. The agent code still doesn't change.
