# 12. Sign in with Microsoft Entra ID (federated login)

ShopAssist users can sign in with their **Microsoft Entra ID** account (formerly Azure AD), next to the
Cognito demo users alice, bob and carol. What a user may do is decided by **directory groups**, so
nobody touches AWS when people join, move or leave.

```
AD-ShopAssist-Prod-Admins  ──assigned to──▶  AppRole-ShopAssist-Admin  ──role mapper──▶  Group-ShopAssist-Admin   (all tools)
AD-ShopAssist-Prod-Viewers ──assigned to──▶  AppRole-ShopAssist-Viewer ──role mapper──▶  Group-ShopAssist-Viewer  (read-only)
in neither group           ──▶  can't sign in to the app (Entra "assignment required"), or 401/403 at the Runtime
```

| Layer | Lives in | You manage it | Example |
|---|---|---|---|
| `AD-` security group | Entra ID (cloud-only groups) | **Yes**: membership = access | `AD-ShopAssist-Prod-Admins` |
| `AppRole-` app role | Entra app registration `ShopAssist-Prod` | Once (definition + group assignment) | `AppRole-ShopAssist-Admin` |
| `Group-` Cognito group | Cognito user pool, `cognito:groups` claim | Never (fixed in code and Cedar) | `Group-ShopAssist-Admin` |

Part 1 (this page, **you**, ~30 minutes in the browser) sets up Entra. Part 2 (Terraform, done by the
pipeline) connects Cognito to it. Nothing here needs any install on your PC.

---

## Part 1: Entra setup (runbook)

Values you'll need, fixed for this project:

| Setting | Value |
|---|---|
| App registration name | `ShopAssist-Prod` |
| **Redirect URI** (Web) | `https://shopassist-prod-438750401181.auth.us-east-1.amazoncognito.com/oauth2/idpresponse` |
| Groups | `AD-ShopAssist-Prod-Admins`, `AD-ShopAssist-Prod-Viewers` |
| App role values | `AppRole-ShopAssist-Admin`, `AppRole-ShopAssist-Viewer` |

The redirect URI uses the Cognito domain, which stays the same across destroy and redeploy (it's
built from the environment and the account id), so you register it once.

### 1. Create a free tenant
1. Go to <https://azure.microsoft.com/free> and create a free Azure account. A card is needed for
   identity verification; Entra ID itself is free. The sign-up creates a **tenant** (your directory),
   for example `yourname.onmicrosoft.com`.
2. Open the **Microsoft Entra admin center**: <https://entra.microsoft.com>.
3. **Overview** → copy the **Tenant ID** (a GUID). You'll give it to Claude.

### 2. Start the Entra ID P2 trial (needed to assign groups to app roles)
1. Entra admin center → **Billing → Licenses → All products → Try / Buy**.
2. **Microsoft Entra ID P2 → Free trial → Activate** (30 days, 100 licenses). It can take a few
   minutes to appear.
3. After the trial: see [After the trial](#after-the-trial-entra-id-free).

### 3. Create the two security groups
**Groups → All groups → New group**, twice:

| Group type | Group name | Membership type |
|---|---|---|
| Security | `AD-ShopAssist-Prod-Admins` | Assigned |
| Security | `AD-ShopAssist-Prod-Viewers` | Assigned |

### 4. Create three test users
**Users → All users → New user → Create new user**, three times. Note each password.

| User principal name | Display name | Group | Expected result |
|---|---|---|---|
| `shop.admin@<tenant>.onmicrosoft.com` | Shop Admin | `AD-ShopAssist-Prod-Admins` | all tools (like alice) |
| `shop.viewer@<tenant>.onmicrosoft.com` | Shop Viewer | `AD-ShopAssist-Prod-Viewers` | read-only (like bob) |
| `shop.nobody@<tenant>.onmicrosoft.com` | Shop Nobody | none | blocked (like carol) |

Then open each group → **Members → Add members** and add the admin and viewer users. Give the users a
P2 license: **Users → (user) → Licenses → Assignments → Microsoft Entra ID P2** (trial licenses).

### 5. Register the app
**Applications → App registrations → New registration**:
- Name: `ShopAssist-Prod`
- Supported account types: **Accounts in this organizational directory only** (single tenant)
- Redirect URI: platform **Web**, the redirect URI from the table above
- **Register.** On the overview page copy the **Application (client) ID**.

### 6. Define the two app roles
In the app registration → **App roles → Create app role**, twice:

| Display name | Allowed member types | Value | Description |
|---|---|---|---|
| ShopAssist Admin | Users/Groups | `AppRole-ShopAssist-Admin` | All ShopAssist tools |
| ShopAssist Viewer | Users/Groups | `AppRole-ShopAssist-Viewer` | Read-only ShopAssist tools |

Keep **"Do you want to enable this app role?"** ticked.

### 7. Sign-in permissions and the email claim
- **API permissions**: keep `User.Read`; **Add a permission → Microsoft Graph → Delegated** →
  tick `openid`, `email`, `profile` → **Add** → **Grant admin consent for <tenant>**.
- **Token configuration → Add optional claim → ID** → tick `email` → Add (accept the Graph prompt).

The `roles` claim needs no setting: Entra puts the user's app roles into the ID token automatically.

### 8. Create the client secret
**Certificates & secrets → Client secrets → New client secret**, description `cognito-prod`, expiry 6
months → **Add** → copy the **Value** immediately (it is shown only once).

Store it as a GitHub secret on the `prod` environment (never in git, never in chat):
**GitHub → Settings → Environments → prod → Environment secrets → Add secret**
- Name: `TF_VAR_ENTRA_CLIENT_SECRET`
- Value: the secret value

Put a reminder in your calendar for the expiry date. A new secret = update this GitHub secret, deploy.

### 9. Assign the groups to the roles (the access gate)
**Applications → Enterprise applications → ShopAssist-Prod**:
1. **Properties → Assignment required? = Yes → Save.** Users who aren't assigned can't even get a
   token for the app.
2. **Users and groups → Add user/group**:
   - Users and groups: `AD-ShopAssist-Prod-Admins`, Select a role: **ShopAssist Admin** → Assign
   - Users and groups: `AD-ShopAssist-Prod-Viewers`, Select a role: **ShopAssist Viewer** → Assign
   - Don't assign `shop.nobody`.

### 10. Hand-off to Claude
Send in chat: the **Tenant ID** and the **Application (client) ID** (neither is a secret). The client
secret goes only into the GitHub environment secret from step 8.

---

## Part 2: what Terraform does with it (for reference)

With `enable_entra = true`, `entra_tenant_id` and `entra_client_id` in `infra/terraform/envs/prod.tfvars`
and the secret from GitHub, the main stack adds:
- a Cognito **OIDC identity provider** `EntraID` (issuer `https://login.microsoftonline.com/<tenant>/v2.0`),
  mapping `email`, `name` and `roles` (into the custom attribute `custom:entra_roles`);
- the **role mapper** Lambda (pre-token-generation trigger) that turns `AppRole-*` into `Group-*`
  in the token, allow-list only, highest role wins, no match = no group (fail closed);
- the users app client: authorization code + PKCE, providers `COGNITO` and `EntraID`, loopback callback
  for the CLI and the chat page, access tokens of 15 minutes (so removed users lose access quickly).

## Taking access away
Remove the user from the `AD-` group. Their next sign-in or token refresh (at most 15 minutes) no
longer carries the role. Immediate: disable the user in Entra and sign them out of Cognito
(`aws cognito-idp admin-user-global-sign-out`).

## After the trial (Entra ID Free)
Entra Free can't assign groups to app roles. Switch `entra_role_source = "groups"` and map the group
**object ids** instead (Groups → the group → Object ID), set Assignment required to No, and add the
`groups` claim (Token configuration → Add groups claim → Security groups). Users in no ShopAssist
group can then sign in to Entra but are still refused by the Runtime (401/403).
