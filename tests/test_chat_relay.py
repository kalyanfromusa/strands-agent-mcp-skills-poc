"""chat_relay: the hosted chat page's Lambda relay to the AgentCore Runtime (no network, urlopen is faked)."""
from __future__ import annotations
import base64
import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("chat_relay", ROOT / "chat_relay" / "handler.py")
cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr)
sys.path.insert(0, str(ROOT / "scripts"))
import chat_ui  # noqa: E402
import invoke_agent  # noqa: E402
ORIGIN = "https://d111.cloudfront.net"
RUNTIME = "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws/invocations?qualifier=DEFAULT"
TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1MSJ9.c2lnbmF0dXJl"
SESSION = "s" * 40
class FakeResp:
    def __init__(self, body: str, status: int = 200, content_type: str = "application/json"):
        self._body = body.encode()
        self.status = status
        self.headers = {"content-type": content_type}
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("RUNTIME_URL", RUNTIME)
    monkeypatch.setenv("ALLOWED_ORIGIN", ORIGIN)
    monkeypatch.setenv("TIMEOUT_SECONDS", "5")
@pytest.fixture
def upstream(monkeypatch):
    """Fake urlopen; `calls` records the requests, `answer` is what to return or raise."""
    state = {"calls": [], "answer": FakeResp('{"response": "hi"}')}
    def fake(request, timeout=None):
        state["calls"].append((request, timeout))
        if isinstance(state["answer"], Exception):
            raise state["answer"]
        return state["answer"]
    monkeypatch.setattr(cr.urllib.request, "urlopen", fake)
    return state
def event(prompt="hello", token=TOKEN, session=SESSION, origin=ORIGIN, content_type="application/json",
          body=None, base64_body=False, method="POST") -> dict:
    headers = {"content-type": content_type, cr.SESSION_HEADER: session}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    if origin is not None:
        headers["origin"] = origin
    raw = json.dumps({"prompt": prompt}) if body is None else body
    if base64_body:
        raw = base64.b64encode(raw.encode()).decode()
    return {"headers": headers, "body": raw, "isBase64Encoded": base64_body,
            "requestContext": {"http": {"method": method}}}
def call(ev):
    out = cr.lambda_handler(ev, None)
    return out["statusCode"], json.loads(out["body"]), out
def test_happy_path_and_passthrough(upstream):
    code, body, out = call(event())
    assert code == 200 and body["status"] == 200 and body["result"] == {"response": "hi"}
    request, timeout = upstream["calls"][0]
    assert request.full_url == RUNTIME and timeout == 5.0
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert request.get_header("X-amzn-bedrock-agentcore-runtime-session-id") == SESSION
    assert json.loads(request.data) == {"prompt": "hello"}
def test_no_origin_header_is_allowed(upstream):
    assert call(event(origin=None))[0] == 200
def test_foreign_origin_refused(upstream):
    code, body, _ = call(event(origin="https://evil.example"))
    assert code == 403 and "error" in body and not upstream["calls"]
def test_base64_body(upstream):
    assert call(event(base64_body=True))[0] == 200
@pytest.mark.parametrize("token", [None, "", "not-a-jwt", "a.b", "a b.c.d", "a." + "b" * 8200 + ".c"])
def test_bad_token(upstream, token):
    code, body, _ = call(event(token=token))
    assert code == 400 and "JWT" in body["error"] and not upstream["calls"]
@pytest.mark.parametrize("session", ["", "short", "x" * 101, "bad session " + "x" * 30])
def test_bad_session(upstream, session):
    code, body, _ = call(event(session=session))
    assert code == 400 and "session" in body["error"] and not upstream["calls"]
@pytest.mark.parametrize("prompt", ["", "   ", "x" * 4001, 5, None])
def test_bad_prompt(upstream, prompt):
    code, body, _ = call(event(prompt=prompt))
    assert code == 400 and "prompt" in body["error"] and not upstream["calls"]
def test_prompt_at_limit_ok(upstream):
    assert call(event(prompt="x" * 4000))[0] == 200
def test_bad_content_type(upstream):
    assert call(event(content_type="text/plain"))[0] == 415
def test_wrong_method(upstream):
    assert call(event(method="GET"))[0] == 405
def test_oversized_and_empty_body(upstream):
    assert call(event(body=json.dumps({"prompt": "x" * 70000})))[0] == 413
    assert call(event(body=""))[0] == 413
def test_body_not_json(upstream):
    assert call(event(body="{nope"))[0] == 400
    assert call(event(body="[1]"))[0] == 400
def test_sse_and_json_parse_like_invoke_agent(upstream):
    sse = 'data: "{\\"response\\": \\"a\\"}"\n\ndata: ignored\n'
    cases = [
        ('{"response": "x", "tools_used": ["t"]}', "application/json"),
        ('"{\\"response\\": \\"wrapped\\"}"', "application/json"),
        ("plain text", "text/plain"),
        ("[1, 2]", "application/json"),
        (sse, "text/event-stream"),
        ('data: {"response": "s1"}\ndata: ', "text/event-stream"),
    ]
    for text, ctype in cases:
        upstream["answer"] = FakeResp(text, content_type=ctype)
        _, body, _ = call(event())
        assert body["result"] == invoke_agent._parse_body(text, ctype)
@pytest.mark.parametrize("status", [401, 403, 500])
def test_upstream_errors_mapped(upstream, status):
    upstream["answer"] = urllib.error.HTTPError(RUNTIME, status, "x", {"content-type": "text/plain"},
                                                io.BytesIO(("e" * 3000).encode()))
    code, body, _ = call(event())
    assert code == 200 and body["status"] == status and len(body["error"]) == 2000 and "result" not in body
def test_timeout_and_connection_errors(upstream):
    upstream["answer"] = TimeoutError()
    assert call(event())[0] == 504
    upstream["answer"] = urllib.error.URLError(TimeoutError())
    assert call(event())[0] == 504
    upstream["answer"] = urllib.error.URLError(ConnectionRefusedError("boom"))
    code, body, _ = call(event())
    assert code == 502 and "boom" not in body["error"]
def test_no_cors_headers(upstream):
    for ev in (event(), event(token="bad"), event(origin="https://evil.example")):
        out = cr.lambda_handler(ev, None)
        assert not [h for h in out["headers"] if h.lower().startswith("access-control-")]
def test_token_never_logged(upstream, capsys, caplog):
    answers = [
        FakeResp("{}"),
        urllib.error.HTTPError(RUNTIME, 401, "x", {}, io.BytesIO(b"denied")),
        TimeoutError(),
        urllib.error.URLError(OSError("down")),
    ]
    for answer in answers:
        upstream["answer"] = answer
        call(event())
    call(event(origin="https://evil.example"))
    call(event(token=TOKEN + "!"))
    seen = capsys.readouterr()
    assert TOKEN not in seen.out + seen.err + caplog.text
    assert "status=200" in seen.out
def test_limits_match_local_chat_ui():
    assert cr.JWT_RE.pattern == chat_ui.JWT_RE.pattern
    assert cr.SESSION_RE.pattern == chat_ui.SESSION_RE.pattern
    assert cr.MAX_PROMPT == chat_ui.MAX_PROMPT
    assert cr.MAX_BODY == chat_ui.MAX_BODY
    assert cr.SESSION_HEADER == invoke_agent.SESSION_HEADER.lower()
