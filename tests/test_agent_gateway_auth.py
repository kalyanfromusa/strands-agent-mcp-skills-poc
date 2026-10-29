"""Generic agent: Gateway bearer-token auth (no network)."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

import gateway_auth as ga  # noqa: E402


def make_jwt(claims: dict) -> str:
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    return f"{b64(json.dumps({'alg': 'none'}).encode())}.{b64(json.dumps(claims).encode())}."


@pytest.fixture(autouse=True)
def clean_relay():
    ga.bind_request_token(None)
    yield
    ga.bind_request_token(None)


def test_bearer_from_header():
    assert ga.bearer_from_header("Bearer abc") == "abc"
    assert ga.bearer_from_header("bearer   abc  ") == "abc"
    assert ga.bearer_from_header("BEARER abc") == "abc"
    assert ga.bearer_from_header("abc") == "abc"
    assert ga.bearer_from_header("Bearer ") is None
    assert ga.bearer_from_header("  bearer") is None
    assert ga.bearer_from_header("") is None
    assert ga.bearer_from_header(None) is None


def test_jwt_claims_unsigned_token():
    claims = {"sub": "u-123", "cognito:groups": ["shop-viewer"], "exp": 1999999999, "pad": "x?>"}
    for n in range(4):  # payload lengths that need 0..3 padding characters
        c = {**claims, "n": "y" * n}
        assert ga.jwt_claims(make_jwt(c)) == c


def test_jwt_claims_not_a_jwt():
    assert ga.jwt_claims(None) == {}
    assert ga.jwt_claims("") == {}
    assert ga.jwt_claims("opaque-token") == {}
    assert ga.jwt_claims("a.!!!notbase64.c") == {}
    assert ga.jwt_claims("a." + base64.urlsafe_b64encode(b"[1,2]").decode() + ".c") == {}


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_cached_token_refreshes_before_expiry():
    clock = FakeClock(1000)
    calls: list[float] = []

    def fetch():
        calls.append(clock.now)
        return f"t{len(calls)}", clock.now + 3600

    src = ga.CachedTokenSource(fetch, margin=300, clock=clock)
    assert src.get_token() == "t1"
    clock.now += 3299  # 301 s before expiry: still cached
    assert src.get_token() == "t1"
    clock.now += 1  # exactly at the margin: refresh
    assert src.get_token() == "t2"
    assert calls == [1000, 4300]
    assert src.get_token() == "t2"


def test_cached_token_uses_jwt_exp_or_default_lifetime():
    clock = FakeClock(0)
    jwt = make_jwt({"exp": 1000})
    src = ga.CachedTokenSource(lambda: (jwt, None), margin=300, clock=clock)
    assert src.get_token() == jwt and src._expires_at == 1000

    opaque = ga.CachedTokenSource(lambda: ("opaque", None), margin=300, clock=clock)
    opaque.get_token()
    assert opaque._expires_at == ga.DEFAULT_TOKEN_LIFETIME_SECONDS


def test_passthrough_source_and_bind_request_token():
    src = ga.PassthroughTokenSource()
    assert src.get_token() is None
    ga.bind_request_token("tok-a")
    assert src.get_token() == "tok-a" and ga.request_token.get() == "tok-a"
    ga.bind_request_token("tok-b")
    assert src.get_token() == "tok-b"


def test_relay_visible_from_other_threads():
    """The MCP client's background thread has a stale contextvar copy; the relay wins."""
    import threading

    ga.bind_request_token("first")
    seen: list[str | None] = []
    ready, go = threading.Event(), threading.Event()

    def worker():
        ready.set()
        go.wait(5)
        seen.append(ga.PassthroughTokenSource().get_token())

    t = threading.Thread(target=worker)
    t.start()
    ready.wait(5)
    ga.bind_request_token("second")
    go.set()
    t.join(5)
    assert seen == ["second"]


def _drive(auth: httpx.Auth) -> httpx.Request:
    request = httpx.Request("POST", "https://gw.example/mcp")
    flow = auth.auth_flow(request)
    sent = next(flow)
    with pytest.raises(StopIteration):
        flow.send(httpx.Response(200))
    return sent


def test_gateway_auth_sets_header():
    ga.bind_request_token("user-token")
    sent = _drive(ga.GatewayAuth(ga.PassthroughTokenSource()))
    assert sent.headers["Authorization"] == "Bearer user-token"


def test_gateway_auth_reads_token_per_request():
    auth = ga.GatewayAuth(ga.PassthroughTokenSource())
    ga.bind_request_token("one")
    assert _drive(auth).headers["Authorization"] == "Bearer one"
    ga.bind_request_token("two")
    assert _drive(auth).headers["Authorization"] == "Bearer two"


def test_gateway_auth_without_token_leaves_header_unset():
    assert "Authorization" not in _drive(ga.GatewayAuth(ga.NoTokenSource())).headers


def test_gateway_auth_async_flow():
    auth = ga.GatewayAuth(ga.CachedTokenSource(lambda: ("cc-token", None), clock=FakeClock(0)))

    async def run() -> httpx.Request:
        flow = auth.async_auth_flow(httpx.Request("GET", "https://gw.example/mcp"))
        sent = await flow.__anext__()
        await flow.aclose()
        return sent

    assert asyncio.run(run()).headers["Authorization"] == "Bearer cc-token"


def test_build_gateway_auth_modes():
    def settings(mode: str, **creds: str) -> SimpleNamespace:
        base = {"cognito_token_url": "", "cognito_client_id": "", "cognito_client_secret": ""}
        return SimpleNamespace(gateway_auth_mode=mode, oauth_scopes=("a/b",), **{**base, **creds})

    assert ga.build_gateway_auth(settings("none")) is None
    passthrough = ga.build_gateway_auth(settings("passthrough"))
    assert isinstance(passthrough, ga.GatewayAuth) and isinstance(passthrough._source, ga.PassthroughTokenSource)
    with pytest.raises(ValueError, match="COGNITO_TOKEN_URL"):
        ga.build_gateway_auth(settings("client_credentials"))
    cc = ga.build_gateway_auth(settings("client_credentials", cognito_token_url="https://idp/token",
                                        cognito_client_id="id", cognito_client_secret="secret"))
    assert isinstance(cc._source, ga.CachedTokenSource)
    with pytest.raises(ValueError):
        ga.build_gateway_auth(settings("weird"))


def test_client_credentials_fetcher_posts_grant(monkeypatch):
    seen = {}

    def fake_post(url, data, auth, timeout):
        seen.update(url=url, data=data, auth=auth)
        return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600}, request=httpx.Request("POST", url))

    monkeypatch.setattr(ga.httpx, "post", fake_post)
    token, expires_at = ga.client_credentials_fetcher("https://idp/token", "id", "secret", ("a/b", "c/d"))()
    assert token == "abc" and expires_at is not None
    assert seen == {"url": "https://idp/token", "data": {"grant_type": "client_credentials", "scope": "a/b c/d"},
                    "auth": ("id", "secret")}
