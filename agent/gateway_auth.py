"""Bearer-token authentication for the MCP connection to the Gateway.

What this file does
    Makes sure every message the agent sends to the AgentCore Gateway carries a login token in the header
    `Authorization: Bearer <token>`. The Gateway rejects messages without a valid token, and it uses the token
    to decide which tools the caller may see and call.

Where it fits
    `app.py` calls `build_gateway_auth()` once at start-up and gives the result to the Strands `MCPClient`
    (the part of the agent that talks to the Gateway). On every request, `app.py` calls
    `bind_request_token()` with the token of the person who sent the request. After that, every message the
    `MCPClient` sends to the Gateway (listing tools, calling a tool) passes through `GatewayAuth`, which adds
    the token.

    `GatewayAuth` is an `httpx.Auth` (a plug-in for the httpx web library) that sets
    `Authorization: Bearer <token>` on every request the MCP client sends. Where the token comes from depends
    on the mode:

    - `passthrough`: the caller's own token, forwarded by the Runtime (request header
      allowlist) and handed to us per invocation with `bind_request_token()`.
    - `client_credentials`: an OAuth2 client-credentials token (local dev), cached until
      about 5 minutes before it expires.
    - `none`: no header (local emulator).

Step by step (passthrough mode, the deployed setup)
    1. A user (for example alice) logs in to Amazon Cognito and gets a token: a signed JWT that says who she is.
    2. She calls the agent with that token. The AgentCore Runtime checks it first: users outside the
       `Group-ShopAssist-Admin` / `Group-ShopAssist-Viewer` groups (carol) are refused there, before any agent code runs. Because the
       Runtime is set up to forward the `Authorization` header (a "request header allowlist"), the agent code
       receives the token.
    3. `app.py` extracts the raw token (`bearer_from_header`) and stores it (`bind_request_token`).
    4. When the agent talks to the Gateway, `GatewayAuth` reads the stored token (`current_request_token`) and
       adds it to the message.
    5. The Gateway checks the token again and applies AgentCore Policy (Cedar rules): alice (admin) sees and
       may call all tools, bob (viewer) only the read-only tools (no `create_return`, no `create_ticket`).
    Why forward the user's token instead of the agent's own login? So the Gateway knows the real person behind
    each tool call and can enforce per-user permissions. The agent itself is given no extra powers.

    Passthrough detail: the token is carried in a contextvar for code running in the
    request's context. Strands' `MCPClient` runs its HTTP traffic on a background thread
    whose context is a copy taken when the client starts, so later invocations' contextvar
    values are not visible there. `bind_request_token()` therefore also publishes the token
    to a process-wide relay that the auth reads. Invocations are serialised per process
    (one Runtime microVM per session) and the agent is rebuilt when the token's `sub`
    changes, so the relay always holds the current caller's token.

    In plain words: a "contextvar" is a per-request note. The Gateway connection runs on its own helper
    thread, which took a photocopy of the notes when it started and never sees newer ones. So the token is
    also written on a shared "notice board" (the relay) that the helper thread can always read. That is safe
    here because only one request runs at a time in each Runtime session, and a new user gets a new agent.

Key terms
    Token / bearer token: a string that proves who you are. "Bearer" means whoever holds it may use it.
    JWT (JSON Web Token): a signed token in three dot-separated parts; the middle part lists "claims" such as
        `sub` (the user's unique id) and `exp` (when it expires).
    OAuth2 client credentials: a way for a program (not a person) to log in with a client id and secret.
    Amazon Cognito: the AWS user directory that issues the tokens (users alice, bob, carol).
    Thread: a separate line of work running at the same time inside one program.
    Lock: a guard that lets only one thread at a time change a shared value.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import logging
import threading
import time
from collections.abc import AsyncGenerator, Callable, Generator
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Get a fresh token when the cached one has less than 5 minutes (300 seconds) left, so it never expires
# in the middle of a conversation.
REFRESH_MARGIN_SECONDS = 300
# If we can't tell when a token expires, assume it is valid for 1 hour (Cognito's usual lifetime).
DEFAULT_TOKEN_LIFETIME_SECONDS = 3600

# Raw bearer token of the current invocation (no "Bearer " prefix).
# The per-request note described in the file header.
request_token: contextvars.ContextVar[str | None] = contextvars.ContextVar("gateway_request_token", default=None)

# The shared "notice board" (relay) that the Gateway connection's helper thread reads, plus its lock.
_relay_lock = threading.Lock()
_relay_token: str | None = None


def bind_request_token(token: str | None) -> contextvars.Token:
    """Make `token` the current caller's token (contextvar plus process-wide relay).

    Called by `app.py` at the start of every invocation in passthrough mode.
    """
    global _relay_token
    # Write to the shared relay (under the lock), then to the per-request note.
    with _relay_lock:
        _relay_token = token
    return request_token.set(token)


def current_request_token() -> str | None:
    """The current caller's token: the contextvar if set in this context, else the relay.

    In practice the relay is checked first (see the comment below); the per-request note is only a fallback
    for when the relay is empty.
    """
    with _relay_lock:
        relayed = _relay_token
    # The relay is authoritative because a background thread's contextvar copy may be stale.
    return relayed if relayed is not None else request_token.get()


def bearer_from_header(value: str | None) -> str | None:
    """Extract the raw token from an `Authorization` header value.

    Accepts "Bearer abc123" (any capitalisation) or just "abc123", and returns "abc123". Returns None when
    there is no token at all.
    """
    if not value:
        return None
    value = value.strip()
    if value.lower() == "bearer":  # "Bearer " with an empty token, already stripped
        return None
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value or None


def jwt_claims(token: str | None) -> dict[str, Any]:
    """Decode a JWT payload WITHOUT verifying it (the Gateway verifies). Returns {} if not a JWT.

    The agent only reads the claims for bookkeeping (who is calling, for the per-user agent cache; when the
    token expires). It never trusts them for security decisions: the Runtime and the Gateway check the
    signature.
    """
    if not token:
        return {}
    # A JWT is "header.payload.signature". The payload (part 2) holds the claims.
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    # The payload is base64url text with its "=" padding removed; put the padding back before decoding.
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


class CachedTokenSource:
    """Caches a token from `fetch` until `margin` seconds before it expires.

    `fetch` returns `(token, expires_at_epoch_seconds_or_None)`. When the expiry is unknown,
    the JWT `exp` claim is used, else a 1 hour lifetime is assumed.

    Used in `client_credentials` mode, so the agent doesn't ask Cognito for a new token on every message.
    `clock` can be replaced in tests to simulate time passing.
    """

    def __init__(
        self,
        fetch: Callable[[], tuple[str, float | None]],
        margin: float = REFRESH_MARGIN_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._fetch = fetch
        self._margin = margin
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def get_token(self) -> str:
        """Return the cached token, fetching a new one first if there is none or it expires soon."""
        # The lock stops two threads from fetching a new token at the same moment.
        with self._lock:
            now = self._clock()
            # Step 1: decide whether the cached token is missing or about to expire.
            if self._token is None or now >= self._expires_at - self._margin:
                # Step 2: fetch a new one and work out when it expires.
                token, expires_at = self._fetch()
                if expires_at is None:
                    exp = jwt_claims(token).get("exp")
                    expires_at = float(exp) if isinstance(exp, (int, float)) else now + DEFAULT_TOKEN_LIFETIME_SECONDS
                # Step 3: remember it for next time.
                self._token, self._expires_at = token, expires_at
                logger.debug("gateway token refreshed, expires in %.0fs", expires_at - now)
            return self._token


class PassthroughTokenSource:
    """Returns the current caller's token (see `bind_request_token`).

    Used in `passthrough` mode: the token always belongs to the person who sent the current request.
    """

    def get_token(self) -> str | None:
        return current_request_token()


class NoTokenSource:
    """A token source that never returns a token (for tests and setups that need no login)."""

    def get_token(self) -> None:
        return None


def client_credentials_fetcher(
    token_url: str, client_id: str, client_secret: str, scopes: tuple[str, ...] | list[str]
) -> Callable[[], tuple[str, float | None]]:
    """OAuth2 client_credentials grant (e.g. Cognito `/oauth2/token`).

    Returns a function that, when called, logs the agent in as itself and returns `(token, expires_at)`.
    The settings are checked now, so a missing value fails at start-up rather than on the first request.
    """
    if not (token_url and client_id and client_secret):
        raise ValueError("client_credentials mode needs COGNITO_TOKEN_URL, COGNITO_CLIENT_ID and COGNITO_CLIENT_SECRET")

    def fetch() -> tuple[str, float | None]:
        # Step 1: ask Cognito's token endpoint for a token, sending the client id and secret as the login and
        # the requested permissions ("scopes", for example "shopassist/invoke").
        data = {"grant_type": "client_credentials"}
        if scopes:
            data["scope"] = " ".join(scopes)
        resp = httpx.post(token_url, data=data, auth=(client_id, client_secret), timeout=15)
        # Step 2: stop with an error if the login failed (wrong secret, wrong URL, ...).
        resp.raise_for_status()
        # Step 3: read the token and turn "expires in N seconds" into an exact expiry time.
        body = resp.json()
        expires_in = body.get("expires_in")
        expires_at = time.time() + float(expires_in) if expires_in else None
        return body["access_token"], expires_at

    return fetch


class GatewayAuth(httpx.Auth):
    """httpx.Auth that injects a fresh Bearer token on every request.

    The httpx web library calls this for each outgoing message to the Gateway. It asks the token source
    (passthrough or cached client credentials) for the current token each time, so a changed caller or a
    refreshed token is picked up straight away.
    """

    def __init__(self, source: Any) -> None:
        self._source = source

    def token(self) -> str | None:
        """Ask the token source for the token to use right now."""
        return self._source.get_token()

    def _apply(self, request: httpx.Request, token: str | None) -> httpx.Request:
        """Add the `Authorization: Bearer <token>` header to the outgoing message (if there is a token)."""
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        return request

    # httpx has two ways of sending: a normal ("sync") one and an "async" one. Both are supported.
    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        yield self._apply(request, self.token())

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        # A token fetch may block on the network, so keep it off the MCP client's event loop.
        token = await asyncio.to_thread(self.token)
        yield self._apply(request, token)


def build_gateway_auth(settings: Any) -> GatewayAuth | None:
    """Create the auth for `settings.gateway_auth_mode`, or None for mode `none`.

    Called once by `app.py` at start-up. The result is attached to the Gateway connection.
    """
    mode = settings.gateway_auth_mode
    if mode == "none":
        return None
    if mode == "passthrough":
        return GatewayAuth(PassthroughTokenSource())
    if mode == "client_credentials":
        fetch = client_credentials_fetcher(
            settings.cognito_token_url, settings.cognito_client_id, settings.cognito_client_secret, settings.oauth_scopes
        )
        return GatewayAuth(CachedTokenSource(fetch))
    raise ValueError(f"unknown GATEWAY_AUTH_MODE {mode!r}")
