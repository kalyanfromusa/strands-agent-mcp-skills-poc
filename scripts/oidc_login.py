"""Browser sign-in (OAuth 2.0 authorization code + PKCE) through Cognito's managed login, incl. Entra ID.

    from oidc_login import browser_login
    token = browser_login(outputs, port=8791, idp="EntraID")     # opens the browser, returns an access token

What this file does
    The helper both scripts/invoke_agent.py (--login) and scripts/chat_ui.py ("Sign in" button) use to
    get a user token without handling any password: the user signs in on Cognito's page, either as a
    Cognito user or with "EntraID" (their Microsoft account), and Cognito redirects back to a small
    one-shot web server on this machine (http://localhost:<port>/callback) with a one-time code, which
    is exchanged for tokens.

Where it fits
    The users app client allows exactly these loopback redirects (infra/terraform/modules/identity,
    callback_urls: 8790 for the chat page, 8791 for the CLI). The access token that comes back carries
    `cognito:groups` (for Entra users written by the role mapper), which the Runtime and Cedar check.

Step by step
    1. Make a PKCE pair: a random secret "verifier" and its SHA-256 "challenge".
    2. Make a random `state` (ties the answer to this request) and `nonce` (ties the ID token to it).
    3. Open <cognito domain>/oauth2/authorize?... in the browser (with identity_provider=EntraID to go
       straight to Microsoft).
    4. Wait for Cognito's redirect to /callback; reject it unless `state` matches.
    5. Exchange the code + verifier at /oauth2/token (only this process knows the verifier).
    6. Check the ID token's `nonce`, then return the access token.

Key terms
    PKCE (Proof Key for Code Exchange): protects the one-time code; a stolen code is useless without the
      verifier that never leaves this process.
    Loopback redirect: the browser is sent back to a server on 127.0.0.1/localhost, i.e. this machine.
    The ID token's signature isn't checked here: it comes straight from Cognito's token endpoint over
      HTTPS, which OpenID Connect accepts for the code flow. The Runtime verifies every token it gets.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

SCOPES = "openid email profile"


def pkce_pair() -> tuple[str, str]:
    """Step 1: (verifier, S256 challenge)."""
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(domain_url: str, client_id: str, redirect_uri: str, state: str, nonce: str,
                  challenge: str, idp: str | None = None) -> str:
    """Step 3: the address of Cognito's sign-in page for this request."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if idp:
        params["identity_provider"] = idp
    return f"{domain_url.rstrip('/')}/oauth2/authorize?{urllib.parse.urlencode(params)}"


def exchange_code(domain_url: str, client_id: str, code: str, redirect_uri: str, verifier: str) -> dict[str, Any]:
    """Step 5: one-time code + verifier -> tokens."""
    import httpx

    resp = httpx.post(
        f"{domain_url.rstrip('/')}/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"token exchange failed (HTTP {resp.status_code}): {resp.text[:300]}")
    return resp.json()


def jwt_claims(token: str) -> dict[str, Any]:
    """Read a JWT's payload (no signature check; see the module notes)."""
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def check_nonce(tokens: dict[str, Any], nonce: str) -> None:
    """Step 6: the ID token must echo our nonce (protects against a replayed sign-in)."""
    id_token = tokens.get("id_token")
    if not id_token or jwt_claims(id_token).get("nonce") != nonce:
        raise RuntimeError("the ID token's nonce doesn't match this sign-in; refusing it")


class PendingLogin:
    """One sign-in in progress: the values to check the callback against (used by chat_ui.py too)."""

    def __init__(self, outputs: dict[str, Any], redirect_uri: str, idp: str | None = None) -> None:
        self.domain_url = outputs["cognito_domain_url"]
        self.client_id = outputs["cognito_users_client_id"]
        self.redirect_uri = redirect_uri
        self.verifier, challenge = pkce_pair()
        self.state = secrets.token_urlsafe(24)
        self.nonce = secrets.token_urlsafe(24)
        self.url = authorize_url(self.domain_url, self.client_id, redirect_uri, self.state, self.nonce, challenge, idp)

    def finish(self, query: dict[str, list[str]]) -> dict[str, Any]:
        """Steps 4-6 for the callback's query string. Returns the token response."""
        if "error" in query:
            raise RuntimeError(f"sign-in failed: {query.get('error', [''])[0]} {query.get('error_description', [''])[0]}")
        if not secrets.compare_digest(query.get("state", [""])[0], self.state):
            raise RuntimeError("state mismatch: this callback isn't for the sign-in we started")
        tokens = exchange_code(self.domain_url, self.client_id, query.get("code", [""])[0], self.redirect_uri, self.verifier)
        check_nonce(tokens, self.nonce)
        return tokens


def browser_login(outputs: dict[str, Any], port: int = 8791, idp: str | None = None, timeout: float = 300) -> str:
    """CLI flow: open the browser, wait for the one callback on localhost:<port>, return the access token."""
    redirect_uri = f"http://localhost:{port}/callback"
    login = PendingLogin(outputs, redirect_uri, idp)
    result: dict[str, Any] = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            try:
                result["tokens"] = login.finish(urllib.parse.parse_qs(parsed.query))
                body, status = b"Signed in. You can close this tab and go back to the terminal.", 200
            except Exception as e:  # shown to the user in the browser and the terminal
                result["error"] = str(e)
                body, status = f"Sign-in failed: {e}".encode(), 400
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, *args: Any) -> None:  # the query string holds the one-time code: don't log it
            pass

    server = HTTPServer(("127.0.0.1", port), Callback)
    server.timeout = timeout
    print(f"Opening the sign-in page in your browser (waiting on {redirect_uri}) ...")
    if not webbrowser.open(login.url):
        print(f"Open this address yourself:\n  {login.url}")
    server.serve_forever()
    server.server_close()
    if "error" in result:
        raise SystemExit(result["error"])
    return result["tokens"]["access_token"]
