# 11. Browser chat UI

`scripts/chat_ui.py` serves a small chat page (`scripts/chat_ui.html`) on `http://127.0.0.1:8790`
and relays each message to an agent. You paste a JWT to choose who you are, then chat. The left panel
has sample conversations for alice, bob and carol, built from `data/seed.json`. Click a turn to send
it, or *Run* to play a whole conversation in order.

| Mode | Command | Agent | Token | Who decides the tools |
|---|---|---|---|---|
| Local | `chat_ui.py --local` | `agent/app.py` on `localhost:8080` | Dummy token from the page (not checked) | `local_gateway.py --role admin\|viewer` |
| Deployed | `chat_ui.py` | AgentCore Runtime from `.deploy/outputs.json` | Real Cognito **access** token | Runtime JWT authorizer + Cedar at the Gateway |

Why a relay: the AgentCore Runtime doesn't answer CORS preflights, so a page can't call it straight
from the browser. The relay listens on `127.0.0.1` only, refuses other Host/Origin headers, and
forwards to one target fixed at start-up, so a web page can't redirect your token elsewhere. It
never logs or stores tokens. They stay in the browser tab's memory.

## Local testing (no AWS deployment)

This is the same setup as `run_local.py` in the [README quick start](../README.md#quick-start), but
with the agent served over HTTP on port 8080, the way the Runtime runs it. You need AWS credentials
that can call Bedrock. Nothing else in AWS is used.

PowerShell, from the repo root, in three terminals:

```powershell
# 1. Gateway emulator with in-memory data. Add --role viewer for bob's read-only tools.
uv run python scripts/local_gateway.py --port 8765

# 2. The agent as an HTTP server on :8080 (the same app.py that runs in AgentCore Runtime)
$env:PYTHONIOENCODING = "utf-8"            # Windows: see "Agent exits silently" below
$env:GATEWAY_AUTH_MODE = "none"
$env:GATEWAY_URL       = "http://localhost:8765/mcp"
$env:SKILLS_DIR        = "$PWD\skills"
$env:AWS_REGION        = "us-east-1"
uv run python agent/app.py

# 3. The chat UI
uv run python scripts/chat_ui.py --local --open
```

bash:

```bash
uv run python scripts/local_gateway.py --port 8765 &
GATEWAY_AUTH_MODE=none GATEWAY_URL=http://localhost:8765/mcp SKILLS_DIR="$PWD/skills" AWS_REGION=us-east-1 \
  uv run python agent/app.py &
uv run python scripts/chat_ui.py --local --open
```

In the page, click **Dummy token: alice (admin)** or **Dummy token: bob (viewer)**. The dummy token only
picks the matching sample tab and labels the chat. Locally:

- **Tools come from the emulator role, not the token.** For bob's experience, restart
  `local_gateway.py` with `--role viewer` (or run a second one on another port and point a second
  `app.py` at it).
- **carol can't be reproduced.** There is no Runtime JWT authorizer locally, so her 401/403 only
  shows against a deployment.
- **Memory is per process.** With `GATEWAY_AUTH_MODE=none` the agent keeps one conversation per
  process, so *New session* doesn't clear it. Restart `app.py` to start fresh.
- **Data is in memory.** A return created by alice stays until you restart `local_gateway.py`.
- **Skill edits** need an `app.py` restart, because skills load when the agent is built.

`--local` takes an optional URL if the agent listens elsewhere: `chat_ui.py --local http://localhost:9000/invocations`.

## Deployed testing

After a Terraform or boto3 deployment (`.deploy/outputs.json` exists):

```powershell
uv run python scripts/chat_ui.py --open
uv run python scripts/chat_ui.py --print-token alice     # in another terminal: prints alice's access token
```

Paste the token and click **Use token**. The page decodes the claims for display (username, groups,
`token_use`, `client_id`, expiry) and warns about an ID token, an expired token, a `client_id` other
than the users client, or a token without an allowed group. Restart `chat_ui.py` after a redeploy,
because it reads `outputs.json` at start-up.

Each user gets a new runtime session id, which is reused for every message. So the agent remembers
the conversation (one microVM per session) until you click *New session*.

## Getting a JWT

The agent runtime accepts **access tokens** issued to the `shopassist-users` app client, and the
user must be in `shop-admin` or `shop-viewer`. ID tokens and M2M (client_credentials) tokens are
rejected. Access tokens are valid for 60 minutes.

### Option A: the helper (local machine)

```powershell
uv run python scripts/chat_ui.py --print-token alice      # or bob, carol
```

It reads the password from `.deploy/users.json` and signs in with `USER_PASSWORD_AUTH`, exactly like
`invoke_agent.py`.

### Option B: AWS console with CloudShell

Use this when you only have console access, or no local AWS CLI.

1. **Pick the region.** In the console, switch to the deployment's region (default `us-east-1`).
2. **Copy the app client id.** Open **Amazon Cognito → User pools → `shopassist-pool` → App
   clients** (left menu under *Applications*; older console layouts have it on the *App integration*
   tab) → `shopassist-users`, and copy the *Client ID*. It's also `cognito_users_client_id` in
   `.deploy/outputs.json`.
3. **Check the user's group (optional).** Open **Users → `alice` → Group memberships**, which should
   show `shop-admin`. bob has `shop-viewer`, and carol has none.
4. **Open CloudShell** (the terminal icon in the console's top bar). It runs with your console
   identity. The sign-in call itself needs no IAM permission, because it's a public client.
5. **Sign in and print the access token:**

   ```bash
   aws cognito-idp initiate-auth \
     --region us-east-1 \
     --auth-flow USER_PASSWORD_AUTH \
     --client-id <client id from step 2> \
     --auth-parameters 'USERNAME=alice,PASSWORD=<password>' \
     --query AuthenticationResult.AccessToken --output text
   ```

   The password is in `.deploy/users.json` on the machine that deployed (with Terraform it's also
   in the state file). Quote the parameters with single quotes, because generated passwords can
   contain shell characters.
6. **Copy the token** (one long `eyJ…` line) into the chat page.

To check what you got, paste the token into the page and look at the decoded claims. `token_use`
must be `access`, and `cognito:groups` must list the group.

**Why not the console's "View login page"?** The Cognito managed login (hosted UI) needs an app
client with OAuth flows and a callback URL. `shopassist-users` only enables
`ALLOW_USER_PASSWORD_AUTH` and `ALLOW_REFRESH_TOKEN_AUTH`, so the login page has nothing to send the
token back to. You could add an authorization-code flow and a callback URL to the client, but that's
a change to the identity setup: Terraform or `deploy.py` would undo it on the next run unless you
add it to the IaC as well.

**Don't** reset a demo user's password in the console to get a token. `.deploy/users.json` would no
longer match, and `invoke_agent.py`, `demo_access.py` and `--print-token` would fail for that user.
If you do reset one, set it back as permanent with
`aws cognito-idp admin-set-user-password --user-pool-id <pool id> --username alice --password '<old password>' --permanent`.

## Sample conversations

The page includes these, with the expected reply and tools for each turn. Replies are paraphrased.
The model words them differently each time.

| User | Conversation | Shows |
|---|---|---|
| alice | Order history (C001), tracking 1042 | `order-tracking`, `list_customer_orders`, `get_order` |
| alice | Return the 1042 headphones, then confirm | Eligibility (5 of 15 days, $199.99), then `create_return` |
| alice | Lamp 1002, beanie 1007, smartwatch 1004, swimsuit 1005 | Refusals the tool explains: window, already returned, electronics window, final sale |
| alice | Delayed 1006 (C003), then ticket status | `create_ticket`, `get_ticket` |
| alice | Shipping FAQ, a poem request | `search_faq`, off-topic redirect |
| bob | Orders of C002, 1008, 1009 | Read-only lookups: processing, cancelled |
| bob | Speaker 1003, items of 1004 | Eligibility is allowed: $59.99; T-shirts $57.00 yes, smartwatch no |
| bob | Return the 1042 headphones, "create it anyway" | No `create_return`: permission explained, no workaround |
| bob | Open a ticket for 1006, then FAQ | No `create_ticket`; `search_faq` still works |
| carol | "Hello" | HTTP 401/403 from the Runtime (deployed only) |

Creating a return changes the data, so after alice returns the 1042 headphones, the next check says
"already returned". Run bob's conversations first, or reset: re-seed with
`uv run python scripts/seed_data.py --region <region>` (deployed), or restart `local_gateway.py` (local).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Page shows "couldn't load /api/config" | Open the page through `chat_ui.py` (`http://127.0.0.1:8790/`), not as a file |
| "Relay error (HTTP 421/403)" | Use `127.0.0.1` or `localhost` with the same port. Other host names and cross-site requests are refused on purpose |
| "couldn't reach the agent: ConnectError" (local) | `agent/app.py` isn't running on port 8080, or it crashed. Check its terminal |
| Agent exits silently on the first message (Windows) | Set `PYTHONIOENCODING=utf-8` before starting `app.py`. Strands' error notes contain `└`, which a cp1252 console can't print, and the process dies |
| Reply says `ExpiredTokenException` / `UnrecognizedClientException` | Your AWS credentials for Bedrock expired or are missing. Refresh them and restart `app.py` |
| Access denied (HTTP 401) for alice/bob (deployed) | Token expired (60 min), it's an ID token, or it's from a previous deployment's pool. Get a new access token |
| "no Authorization header reached the agent" (deployed) | The runtime's header allowlist is missing. See [09](09-troubleshooting.md#missing-authorization-header) |
| A reply takes 10–30 s on the first message | Cold start (microVM, S3 skill sync, Gateway tool listing). Later messages are faster |
