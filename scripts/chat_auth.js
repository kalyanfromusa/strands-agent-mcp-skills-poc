/* Browser sign-in helpers for the hosted chat page: OAuth 2.0 authorization code + PKCE on Cognito.
 *
 * What this file does
 *   Mirrors scripts/oidc_login.py (same parameters, same checks) for the page served from CloudFront.
 *   A classic script that exposes globalThis.ChatAuth, so the page loads it with <script src> and
 *   tests/test_chat_auth.py loads it with node.
 *
 * Step by step
 *   1. pkcePair(): random verifier + its SHA-256 challenge.
 *   2. authorizeUrl(): the address of Cognito's sign-in page (identity_provider=EntraID skips the chooser).
 *   3. parseCallback(): read ?code=&state= on the way back; refuse an error or a state mismatch.
 *   4. exchangeCode(): code + verifier -> tokens at /oauth2/token (public client, no secret).
 *   5. checkNonce(): the ID token must echo our nonce.
 *   6. logoutUrl(): Cognito's /logout, which ends the Cognito session and returns to the page.
 *
 * The page keeps verifier, state and nonce in sessionStorage until the callback and the access token
 * only in memory. Nothing here stores anything.
 */
(function (root) {
  "use strict";

  const SCOPES = "openid email profile";

  function b64url(bytes) {
    let s = "";
    for (const b of bytes) s += String.fromCharCode(b);
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function randomString(nBytes) {
    const b = new Uint8Array(nBytes);
    root.crypto.getRandomValues(b);
    return b64url(b);
  }

  /** S256 challenge for a verifier: base64url(sha256(verifier)). */
  async function challengeFor(verifier) {
    const digest = await root.crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
    return b64url(new Uint8Array(digest));
  }

  /** Step 1: {verifier, challenge}. 64 random bytes give an 86-character verifier (RFC 7636: 43..128). */
  async function pkcePair() {
    const verifier = randomString(64);
    return { verifier, challenge: await challengeFor(verifier) };
  }

  /** Step 2. cfg: {cognito_domain_url, client_id}. */
  function authorizeUrl(cfg, redirectUri, state, nonce, challenge, idp) {
    const p = new URLSearchParams({
      response_type: "code",
      client_id: cfg.client_id,
      redirect_uri: redirectUri,
      scope: SCOPES,
      state: state,
      nonce: nonce,
      code_challenge: challenge,
      code_challenge_method: "S256",
    });
    if (idp) p.set("identity_provider", idp);
    return cfg.cognito_domain_url.replace(/\/+$/, "") + "/oauth2/authorize?" + p.toString();
  }

  /** Step 3: {code} for a good callback; throws on an error answer, a missing code or a state mismatch. */
  function parseCallback(search, expectedState) {
    const q = new URLSearchParams(search);
    if (q.get("error")) {
      throw new Error("sign-in failed: " + q.get("error") + " " + (q.get("error_description") || ""));
    }
    if (!expectedState || q.get("state") !== expectedState) {
      throw new Error("state mismatch: this callback isn't for the sign-in we started");
    }
    const code = q.get("code");
    if (!code) throw new Error("the callback has no code");
    return { code: code };
  }

  /** Step 4: tokens (access_token, id_token, ...). */
  async function exchangeCode(cfg, code, redirectUri, verifier) {
    const resp = await root.fetch(cfg.cognito_domain_url.replace(/\/+$/, "") + "/oauth2/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "authorization_code",
        client_id: cfg.client_id,
        code: code,
        redirect_uri: redirectUri,
        code_verifier: verifier,
      }).toString(),
    });
    if (!resp.ok) throw new Error("token exchange failed (HTTP " + resp.status + ")");
    return resp.json();
  }

  function jwtClaims(token) {
    let b64 = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    b64 += "=".repeat((4 - (b64.length % 4)) % 4);
    return JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))));
  }

  /** Step 5. */
  function checkNonce(tokens, nonce) {
    if (!tokens.id_token || jwtClaims(tokens.id_token).nonce !== nonce) {
      throw new Error("the ID token's nonce doesn't match this sign-in; refusing it");
    }
  }

  /** Step 6. */
  function logoutUrl(cfg, redirectUri) {
    const p = new URLSearchParams({ client_id: cfg.client_id, logout_uri: redirectUri });
    return cfg.cognito_domain_url.replace(/\/+$/, "") + "/logout?" + p.toString();
  }

  root.ChatAuth = { SCOPES, randomString, challengeFor, pkcePair, authorizeUrl, parseCallback, exchangeCode, checkNonce, logoutUrl };
})(globalThis);
