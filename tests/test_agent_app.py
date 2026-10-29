"""Generic agent: app helpers, settings, import side effects, and the "no domain knowledge" guard."""

from __future__ import annotations

import ast
import base64
import io
import json
import keyword
import re
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
sys.path.insert(0, str(AGENT))

import app  # noqa: E402
import gateway_auth  # noqa: E402
from config import AUTH_MODES, DEFAULT_GATEWAY_URL, DEFAULT_MODEL_ID, Settings  # noqa: E402


def make_jwt(claims: dict) -> str:
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    return f"{b64(b'{}')}.{b64(json.dumps(claims).encode())}."


# -- app helpers -------------------------------------------------------------------------------


def test_should_rebuild():
    assert app.should_rebuild(None, "u1", has_agent=False)
    assert app.should_rebuild("u1", "u1", has_agent=False)
    assert not app.should_rebuild("u1", "u1", has_agent=True)
    assert app.should_rebuild("u1", "u2", has_agent=True)


def test_caller_key_per_sub_in_passthrough():
    alice, bob = make_jwt({"sub": "sub-alice"}), make_jwt({"sub": "sub-bob"})
    assert app.caller_key("passthrough", alice) == "sub-alice"
    assert app.caller_key("passthrough", bob) == "sub-bob"
    assert app.caller_key("passthrough", make_jwt({"sub": "sub-alice", "jti": "other"})) == "sub-alice"
    assert app.caller_key("passthrough", make_jwt({"client_id": "m2m"})) == "m2m"
    assert app.caller_key("passthrough", "opaque") == "unknown"


def test_caller_key_other_modes_ignore_token():
    assert app.caller_key("none", make_jwt({"sub": "x"})) == "none"
    assert app.caller_key("client_credentials", None) == "client_credentials"


def test_find_header_case_insensitive():
    headers = {"authorization": "Bearer a", "X-Other": "1"}
    assert app.find_header(headers, "Authorization") == "Bearer a"
    assert app.find_header({"AUTHORIZATION": "Bearer b"}, "authorization") == "Bearer b"
    assert app.find_header(headers, "x-other") == "1"
    assert app.find_header(headers, "Missing") is None
    assert app.find_header(None, "Authorization") is None
    assert app.find_header({}, "Authorization") is None


def test_authorization_from_context():
    ctx = type("Ctx", (), {"request_headers": {"authorization": "Bearer ctx"}})()
    assert app._authorization_from(ctx) == "Bearer ctx"


def test_entrypoint_rejects_bad_payload():
    for payload in ({}, {"prompt": "   "}, {"prompt": 3}, "text"):
        out = app.invoke(payload)
        assert out["response"].startswith("Error: payload must be") and out["tools_used"] == []


def test_passthrough_without_token_does_not_build_agent():
    runtime = app.AgentRuntime(Settings(gateway_auth_mode="passthrough"))
    out = runtime.invoke("hello", authorization=None)
    assert out["error"] == "missing_authorization" and runtime._agent is None


def test_system_prompt_is_generic():
    assert "skills" in app.SYSTEM_PROMPT and "Never invent data" in app.SYSTEM_PROMPT


# -- Settings ----------------------------------------------------------------------------------


def test_settings_defaults():
    s = Settings.from_env({})
    assert s.gateway_auth_mode == "passthrough"
    assert s.model_id == DEFAULT_MODEL_ID and s.gateway_url == DEFAULT_GATEWAY_URL
    assert s.region == "us-east-1" and s.skills_dir == "./skills" and s.skills_s3_uri == ""
    assert s.oauth_scopes == ("shopassist/invoke",) and s.mcp_startup_timeout == 30


def test_settings_from_env_values():
    s = Settings.from_env({
        "GATEWAY_AUTH_MODE": " Client_Credentials ", "MODEL_ID": "m", "AWS_DEFAULT_REGION": "eu-west-1",
        "GATEWAY_URL": "https://gw/mcp", "OAUTH_SCOPES": "a/b  c/d", "COGNITO_CLIENT_SECRET": "s3cret",
        "SKILLS_S3_URI": "s3://b/skills/", "SKILLS_CACHE_DIR": "/x", "MCP_STARTUP_TIMEOUT": "5",
    })
    assert s.gateway_auth_mode == "client_credentials" and s.model_id == "m" and s.region == "eu-west-1"
    assert s.oauth_scopes == ("a/b", "c/d") and s.skills_s3_uri == "s3://b/skills/" and s.skills_cache_dir == "/x"
    assert s.mcp_startup_timeout == 5
    assert "s3cret" not in repr(s)
    assert Settings.from_env({"AWS_REGION": "ap-south-1", "AWS_DEFAULT_REGION": "x"}).region == "ap-south-1"


@pytest.mark.parametrize("mode", AUTH_MODES)
def test_settings_accepts_modes(mode):
    assert Settings.from_env({"GATEWAY_AUTH_MODE": mode.upper()}).gateway_auth_mode == mode


def test_settings_rejects_unknown_mode():
    with pytest.raises(ValueError, match="GATEWAY_AUTH_MODE"):
        Settings.from_env({"GATEWAY_AUTH_MODE": "sigv4"})


def test_settings_rejects_bad_timeout():
    with pytest.raises(ValueError):
        Settings.from_env({"MCP_STARTUP_TIMEOUT": "soon"})


# -- import side effects -----------------------------------------------------------------------

_IMPORT_PROBE = r"""
import socket, sys
import botocore.client

def _no_network(*a, **k):
    raise AssertionError("network access at import time")

def _no_aws(*a, **k):
    raise AssertionError("AWS API call at import time")

socket.socket.connect = _no_network
socket.create_connection = _no_network
botocore.client.BaseClient._make_api_call = _no_aws
sys.path.insert(0, sys.argv[1])
import app
assert app._runtime is None, "the runtime must be built lazily"
assert callable(app.invoke)
print("ok")
"""


def test_import_app_makes_no_aws_or_network_calls(tmp_path):
    env = {"PATH": "", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
           "AWS_ACCESS_KEY_ID": "invalid", "AWS_SECRET_ACCESS_KEY": "invalid", "AWS_EC2_METADATA_DISABLED": "true"}
    proc = subprocess.run([sys.executable, "-c", _IMPORT_PROBE, str(AGENT)], capture_output=True, text=True,
                          env=env, cwd=tmp_path, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("ok")


def test_import_does_not_bind_a_token():
    assert gateway_auth.request_token.get() is None


# -- "generic agent" guard ---------------------------------------------------------------------

DOMAIN_STEM = re.compile(r"^(order|return|refund|ticket|shop|customer)")
# The deployment's project prefix (design.md section 1) appears in connection defaults
# such as the OAuth provider name; it names the deployment, not domain logic.
PROJECT_PREFIX = "shopassist"


def _docstring_positions(tree: ast.AST) -> set[tuple[int, int]]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                out.add((first.lineno, first.col_offset))
    return out


def domain_words(source: str) -> list[tuple[int, str]]:
    """Domain words in identifiers and string literals, ignoring comments, docstrings and keywords."""
    docstrings = _docstring_positions(ast.parse(source))
    text_tokens = {tokenize.NAME, tokenize.STRING}
    for name in ("FSTRING_MIDDLE", "TSTRING_MIDDLE"):
        if hasattr(tokenize, name):
            text_tokens.add(getattr(tokenize, name))
    hits = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type not in text_tokens:
            continue
        if tok.type == tokenize.STRING and tok.start in docstrings:
            continue
        if tok.type == tokenize.NAME and keyword.iskeyword(tok.string):
            continue  # `return` the statement is fine
        text = re.sub(r"([a-z])([A-Z])", r"\1 \2", tok.string).lower().replace(PROJECT_PREFIX, " ")
        hits += [(tok.start[0], w) for w in re.findall(r"[a-z]+", text) if DOMAIN_STEM.match(w)]
    return hits


def test_domain_word_scanner_catches_code_but_not_comments():
    src = '"""Docstring about orders."""\n# a refund comment\ndef f(x):\n    """Return the ticket."""\n' \
          '    customer_id = x\n    return f"order {customer_id}"\n'
    assert sorted(w for _, w in domain_words(src)) == ["customer", "customer", "order"]


def test_agent_code_has_no_domain_words():
    files = sorted(AGENT.glob("*.py"))
    assert {p.name for p in files} >= {"app.py", "config.py", "gateway_auth.py", "skill_binding.py",
                                       "skill_source.py", "tools.py"}
    hits = {p.name: h for p in files if (h := domain_words(p.read_text(encoding="utf-8")))}
    assert hits == {}, f"domain words in generic agent code: {hits}"
