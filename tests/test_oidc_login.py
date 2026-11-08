"""scripts/oidc_login.py: PKCE, authorize URL, state and nonce checks (token endpoint stubbed)."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import oidc_login  # noqa: E402

OUTPUTS = {"cognito_domain_url": "https://shopassist-prod-1.auth.us-east-1.amazoncognito.com", "cognito_users_client_id": "abc123"}


def fake_jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


def test_pkce_challenge_is_s256_of_verifier():
    v, c = oidc_login.pkce_pair()
    assert 43 <= len(v) <= 128
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def test_authorize_url_has_everything():
    login = oidc_login.PendingLogin(OUTPUTS, "http://localhost:8791/callback", idp="EntraID")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(login.url).query)
    assert login.url.startswith(OUTPUTS["cognito_domain_url"] + "/oauth2/authorize?")
    assert q["response_type"] == ["code"] and q["code_challenge_method"] == ["S256"]
    assert q["identity_provider"] == ["EntraID"] and q["state"] == [login.state] and q["nonce"] == [login.nonce]
    assert q["redirect_uri"] == ["http://localhost:8791/callback"]


def test_state_mismatch_is_refused(monkeypatch):
    login = oidc_login.PendingLogin(OUTPUTS, "http://localhost:8791/callback")
    monkeypatch.setattr(oidc_login, "exchange_code", lambda *a: pytest.fail("must not exchange"))
    with pytest.raises(RuntimeError, match="state mismatch"):
        login.finish({"code": ["c"], "state": ["someone-else"]})


def test_nonce_checked(monkeypatch):
    login = oidc_login.PendingLogin(OUTPUTS, "http://localhost:8791/callback")
    monkeypatch.setattr(oidc_login, "exchange_code", lambda *a: {"access_token": "AT", "id_token": fake_jwt({"nonce": "wrong"})})
    with pytest.raises(RuntimeError, match="nonce"):
        login.finish({"code": ["c"], "state": [login.state]})
    monkeypatch.setattr(oidc_login, "exchange_code", lambda *a: {"access_token": "AT", "id_token": fake_jwt({"nonce": login.nonce})})
    assert login.finish({"code": ["c"], "state": [login.state]})["access_token"] == "AT"


def test_error_from_cognito_is_reported():
    login = oidc_login.PendingLogin(OUTPUTS, "http://localhost:8791/callback")
    with pytest.raises(RuntimeError, match="access_denied"):
        login.finish({"error": ["access_denied"], "error_description": ["user not assigned"]})
