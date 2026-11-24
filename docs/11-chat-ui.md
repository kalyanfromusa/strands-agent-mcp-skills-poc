# 11. Browser chat UI

`scripts/chat_ui.py` serves a small chat page (`scripts/chat_ui.html`) on `http://127.0.0.1:8790`
and relays each message to an agent. You paste a JWT to choose who you are, then chat. The left panel
has sample conversations for alice, bob and carol, built from `data/seed.json`. Click a turn to send
it, or *Run* to play a whole conversation in order.

| Mode | Command | Agent | Token | Who decides the tools |
|---|---|---|---|---|
| Local | `chat_ui.py --local` | `agent/app.py` on `localhost:8080` | Dummy token from the page (not checked) | `local_gateway.py --role admin\|viewer` |
| Deployed | `chat_ui.py` | AgentCore Runtime from `.deploy/<env>/outputs.json` | Real Cognito **access** token | Runtime JWT authorizer + Cedar at the Gateway |

Why a relay: the AgentCore Runtime doesn't answer CORS preflights, so a page can't call it straight
from the browser. The relay listens on `127.0.0.1` only, refuses other Host/Origin headers, and
forwards to one target fixed at start-up, so a web page can't redirect your token elsewhere. It
never logs or stores tokens. They stay in the browser tab's memory.

## Hosted chat UI (CloudFront)

Each deployed environment also publishes the same page on the internet, so you need no local server,
no `.deploy/` files and no pasted token. Open the address and sign in.

```powershell
# the address is chat_ui_url in .deploy/prod/outputs.json (a pipeline deploy deletes that file at the end of the job;
# recreate it as described in docs/04 §4), or in the deploy job's Terraform outputs:
Get-Content .deploy/prod/outputs.json | ConvertFrom-Json | Select-Object -ExpandProperty chat_ui_url
```

It looks like `https://d1234abcdef.cloudfront.net/`. The page offers **Sign in** with the Cognito page (alice,
bob, carol, passwords in `.deploy/<env>/users.json`) and, when Entra is enabled, **Sign in with Entra ID**.
The access token lasts 15 minutes; after that the page says so and you sign in again. **Sign out** ends the
Cognito session and returns to the page.

How it works:

| Piece | What it is |
|---|---|
| Page | `scripts/chat_ui.html` (published as `index.html`) and `scripts/chat_auth.js`, in a private S3 bucket behind CloudFront. Terraform uploads them, together with a `config.json` that holds the Cognito web client id and the relay address (no secrets). The deploy job then invalidates the CloudFront cache |
| Sign-in | Authorization code + PKCE straight from the browser against the Cognito web client `shopassist-<env>-web` (a public client, callback URL = the page address) |
| Relay | A Lambda function URL (`chat_relay_url`). The page posts `{"prompt"}` to it with your token and session id; it forwards them to the agent Runtime and returns the answer. The Runtime doesn't answer CORS preflights, so a browser can't call it directly |
| Access rights | Unchanged: the Runtime JWT authorizer (group check) and Cedar at the Gateway decide, with your own token. The relay is public but holds no rights of its own |

Sign-in flow (PKCE), in short:

1. The page makes a random `verifier` and sends `SHA-256(verifier)` (the *challenge*), a `state` and a `nonce` to
   the Cognito login page. The verifier, state and nonce are kept in `sessionStorage` only.
2. You sign in. Cognito redirects back to the page with a one-time `code` and your `state`.
3. The page checks the `state`, removes the code from the address bar, and sends `code` + `verifier` to Cognito's
   token endpoint. Cognito hashes the verifier and compares it with the challenge from step 1, so a stolen
   code is useless without the verifier.
4. Cognito returns tokens. The page checks the `nonce` and keeps the access token in memory only
   (never `localStorage` or cookies); the verifier, state and nonce are deleted.

Limits: the relay's reserved concurrency (`chat_relay_reserved_concurrency`, default 5) caps parallel
requests and so the spend; extra requests get HTTP 429. There is no custom domain, WAF or API Gateway.

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

After a deployment, once `.deploy/prod/outputs.json` exists on your machine ([04 §4](04-deploy-terraform.md#4-getting-deployenv-on-your-machine)):

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

The agent runtime accepts **access tokens** issued to the `shopassist-<env>-users` app client, and the
user must be in `Group-ShopAssist-Admin` or `Group-ShopAssist-Viewer`. ID tokens and M2M (client_credentials) tokens are
rejected. Access tokens are valid for 15 minutes.

### Option A: the helper (local machine)

```powershell
uv run python scripts/chat_ui.py --print-token alice      # or bob, carol
```

It reads the password from `.deploy/<env>/users.json` and signs in with `USER_PASSWORD_AUTH`, exactly like
`invoke_agent.py`.

### Option B: AWS console with CloudShell

Use this when you only have console access, or no local AWS CLI.

1. **Pick the region.** In the console, switch to the deployment's region (default `us-east-1`).
2. **Copy the app client id.** Open **Amazon Cognito → User pools → `shopassist-prod-pool` → App
   clients** (left menu under *Applications*; older console layouts have it on the *App integration*
   tab) → `shopassist-prod-users`, and copy the *Client ID*. It's also `cognito_users_client_id` in
   `.deploy/prod/outputs.json`.
3. **Check the user's group (optional).** Open **Users → `alice` → Group memberships**, which should
   show `Group-ShopAssist-Admin`. bob has `Group-ShopAssist-Viewer`, and carol has none.
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

   The password is in `.deploy/prod/users.json` (recreate it as in [04 §4](04-deploy-terraform.md#4-getting-deployenv-on-your-machine);
   it's also in the Terraform state). Quote the parameters with single quotes, because generated passwords can
   contain shell characters.
6. **Copy the token** (one long `eyJ…` line) into the chat page.

To check what you got, paste the token into the page and look at the decoded claims. `token_use`
must be `access`, and `cognito:groups` must list the group.

**Browser sign-in instead of a pasted token.** The users client also allows the authorization-code flow with
PKCE (loopback callback URLs), so `chat_ui.py` has a **Sign in** button and `invoke_agent.py --login` opens the
Cognito managed login (`scripts/oidc_login.py`). With `enable_entra = true` the login page also offers Entra ID
([12-entra-federation.md](12-entra-federation.md)).

**Don't** reset a demo user's password in the console to get a token. `.deploy/<env>/users.json` would no
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
