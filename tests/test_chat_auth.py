"""scripts/chat_auth.js (PKCE helpers of the hosted page) against scripts/oidc_login.py, plus static page checks.

The node tests run chat_auth.js in a real JS engine and skip when `node` is missing.
"""

import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
AUTH_JS = SCRIPTS / "chat_auth.js"
HTML = (SCRIPTS / "chat_ui.html").read_text(encoding="utf-8")

sys.path.insert(0, str(SCRIPTS))
import oidc_login  # noqa: E402

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

CFG = {"cognito_domain_url": "https://auth.example.test/", "client_id": "web-client"}


def run_node(body: str):
    """Load chat_auth.js, run `body` (an async function body that returns JSON) and return the parsed result."""
    script = (
        f"require({json.dumps(str(AUTH_JS))});\n"
        "const A = globalThis.ChatAuth;\n"
        f"(async () => {{ {body} }})().then((r) => console.log(JSON.stringify(r)), (e) => {{ console.error(e); process.exit(1); }});"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_rfc7636_vector():
    r = run_node("return await A.challengeFor('dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk');")
    assert r == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


@needs_node
def test_pkce_pair_matches_python_math():
    r = run_node("return await A.pkcePair();")
    verifier, challenge = r["verifier"], r["challenge"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43,128}", verifier)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    py_verifier, py_challenge = oidc_login.pkce_pair()
    assert len(py_verifier) == len(verifier)
    assert len(py_challenge) == len(challenge)


@needs_node
def test_authorize_url_has_same_query_keys_as_python():
    js = run_node(f"return A.authorizeUrl({json.dumps(CFG)}, 'https://cf.test/', 'st', 'no', 'ch', 'EntraID');")
    py = oidc_login.authorize_url(CFG["cognito_domain_url"], CFG["client_id"], "https://cf.test/", "st", "no", "ch", "EntraID")
    assert js == py or urllib.parse.parse_qs(urllib.parse.urlparse(js).query) == urllib.parse.parse_qs(urllib.parse.urlparse(py).query)
    assert js.startswith("https://auth.example.test/oauth2/authorize?")
    js_no_idp = run_node(f"return A.authorizeUrl({json.dumps(CFG)}, 'https://cf.test/', 'st', 'no', 'ch', null);")
    assert "identity_provider" not in js_no_idp


@needs_node
def test_parse_callback():
    r = run_node(
        """
        const out = {};
        out.ok = A.parseCallback('?code=abc&state=s1', 's1');
        for (const [k, q, s] of [['err', '?error=access_denied&error_description=no&state=s1', 's1'],
                                 ['mismatch', '?code=abc&state=other', 's1'],
                                 ['nostate', '?code=abc', null],
                                 ['nocode', '?state=s1', 's1']]) {
          try { A.parseCallback(q, s); out[k] = 'accepted'; } catch (e) { out[k] = e.message; }
        }
        return out;
        """
    )
    assert r["ok"] == {"code": "abc"}
    assert "access_denied" in r["err"]
    assert "state mismatch" in r["mismatch"]
    assert "state mismatch" in r["nostate"]
    assert "no code" in r["nocode"]


@needs_node
def test_check_nonce_and_logout_url():
    r = run_node(
        """
        const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
        const idt = (n) => b64({alg: 'none'}) + '.' + b64({nonce: n}) + '.x';
        const out = {};
        A.checkNonce({id_token: idt('n1')}, 'n1');
        for (const [k, t] of [['wrong', {id_token: idt('zzz')}], ['missing', {}]]) {
          try { A.checkNonce(t, 'n1'); out[k] = 'accepted'; } catch (e) { out[k] = e.message; }
        }
        out.logout = A.logoutUrl(%s, 'https://cf.test/');
        return out;
        """
        % json.dumps(CFG)
    )
    assert "nonce" in r["wrong"] and "nonce" in r["missing"]
    q = urllib.parse.urlparse(r["logout"])
    assert q.path == "/logout"
    assert urllib.parse.parse_qs(q.query) == {"client_id": ["web-client"], "logout_uri": ["https://cf.test/"]}


def test_page_never_persists_the_token():
    for forbidden in ("localStorage", "document.cookie"):
        assert forbidden not in HTML
        assert forbidden not in AUTH_JS.read_text(encoding="utf-8")
    # sessionStorage only holds the PKCE verifier, state and nonce.
    assert set(re.findall(r"sessionStorage\.(?:set|get|remove)Item\(([^,)]+)", HTML)) <= {"SS.verifier", "SS.state", "SS.nonce", "k"}
    assert "sessionStorage.setItem(SS.verifier" in HTML
    assert "sessionStorage.setItem(state.token" not in HTML


def test_page_loads_auth_script_and_keeps_local_request_shape():
    assert '<script src="chat_auth.js"></script>' in HTML
    assert 'fetch("/api/invoke"' in HTML
    assert "JSON.stringify({ token: state.token, session_id: state.sessionId, prompt })" in HTML
    assert 'fetch("/api/config")' in HTML


def test_chat_ui_serves_auth_js_and_allows_self_scripts():
    import chat_ui

    assert chat_ui.AUTH_JS == AUTH_JS
    assert "script-src 'self' 'unsafe-inline'" in chat_ui.CSP
    src = (SCRIPTS / "chat_ui.py").read_text(encoding="utf-8")
    assert 'path == "/chat_auth.js"' in src
