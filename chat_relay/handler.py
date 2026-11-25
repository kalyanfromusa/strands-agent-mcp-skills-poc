"""chat_relay: the hosted chat page's relay to the AgentCore Runtime (Lambda function URL).

What this file does
  The hosted chat page (CloudFront + S3) runs in the user's browser. The AgentCore Runtime does not
  answer CORS preflights, so the browser can't call it directly. This function URL sits in between:
  it checks the request, forwards the user's own Cognito access token unchanged to the Runtime, and
  returns the reply in the same shape as `scripts/chat_ui.py` `/api/invoke`.

Request (POST to the function URL)
  Headers  authorization: Bearer <jwt>, content-type: application/json,
           x-amzn-bedrock-agentcore-runtime-session-id: <33-100 chars of letters, digits, - or _>
  Body     {"prompt": "..."}
Reply
  HTTP 200 {"status": <Runtime HTTP status>, "elapsed_ms": n, "result": {...}}   Runtime answered < 400
  HTTP 200 {"status": <Runtime HTTP status>, "elapsed_ms": n, "error": "..."}    Runtime answered >= 400
  HTTP 400/403/405/413/415 {"error": "..."}                                       this function refused
  HTTP 502/504 {"error": "..."}                                                   Runtime unreachable / too slow

Security choices
  - The token is never decoded, stored or logged. Only the Runtime's HTTP status and elapsed ms are logged.
  - Who may use the agent is decided by the Runtime's JWT authorizer and the Gateway's Cedar policies,
    not here (the function URL has authorization type NONE).
  - No Access-Control-* headers are emitted: the function URL's own `cors` block does that, and a second
    set would be duplicated. As defense in depth, a request whose Origin header differs from
    ALLOWED_ORIGIN is refused with 403.
  - The target URL comes from the environment (RUNTIME_URL), never from the request.

Environment: RUNTIME_URL (the Runtime /invocations URL), ALLOWED_ORIGIN (https://<cloudfront domain>),
TIMEOUT_SECONDS (upstream timeout, default 280). Python standard library only.

The limits and `_parse_body` are copies of scripts/chat_ui.py and scripts/invoke_agent.py; the tests
(tests/test_chat_relay.py) fail if they drift apart.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"
MAX_BODY = 64 * 1024
MAX_PROMPT = 4000
MAX_TOKEN = 8192
JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{33,100}$")


def _reply(status: int, data: dict[str, Any]) -> dict[str, Any]:
    """A function URL response. Deliberately no Access-Control-* headers (the function URL adds them)."""
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"},
        "body": json.dumps(data),
    }


def _parse_body(text: str, content_type: str) -> dict[str, Any]:
    """Copy of scripts/invoke_agent.py _parse_body: plain JSON, "data:" stream, or JSON inside a string."""
    if "text/event-stream" in content_type:
        chunks = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = "".join(chunks)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"response": text}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return {"response": data}
    return data if isinstance(data, dict) else {"response": str(data)}


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    headers = {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}

    # Step 1: defense in depth for CORS. A browser always sends Origin on a cross-origin POST.
    origin = headers.get("origin")
    if origin is not None and origin != os.environ.get("ALLOWED_ORIGIN", ""):
        return _reply(403, {"error": "origin not allowed"})

    # Step 2: method, content type and body size.
    method = ((event.get("requestContext") or {}).get("http") or {}).get("method", "POST")
    if method != "POST":
        return _reply(405, {"error": "POST only"})
    if "application/json" not in headers.get("content-type", ""):
        return _reply(415, {"error": "Content-Type must be application/json"})
    raw = event.get("body") or ""
    try:
        raw_bytes = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode("utf-8")
    except ValueError:
        return _reply(400, {"error": "body is not valid base64"})
    if not raw_bytes or len(raw_bytes) > MAX_BODY:
        return _reply(413, {"error": f"body must be 1..{MAX_BODY} bytes"})
    try:
        req = json.loads(raw_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _reply(400, {"error": "body is not JSON"})
    if not isinstance(req, dict):
        return _reply(400, {"error": "body must be a JSON object"})

    # Step 3: the token, session id and prompt (same checks as scripts/chat_ui.py).
    token = headers.get("authorization", "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    session_id = headers.get(SESSION_HEADER, "")
    prompt = req.get("prompt")
    if not JWT_RE.match(token) or len(token) > MAX_TOKEN:
        return _reply(400, {"error": "token doesn't look like a JWT (header.payload.signature)"})
    if not SESSION_RE.match(session_id):
        return _reply(400, {"error": "session id must be 33-100 chars of letters, digits, '-' or '_'"})
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
        return _reply(400, {"error": f"prompt must be 1..{MAX_PROMPT} characters"})

    # Step 4: forward to the Runtime with the user's own token.
    timeout = float(os.environ.get("TIMEOUT_SECONDS", "280"))
    request = urllib.request.Request(
        os.environ["RUNTIME_URL"],
        data=json.dumps({"prompt": prompt}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            SESSION_HEADER: session_id,
        },
    )
    started = time.monotonic()
    status = 0
    content_type = ""
    text = ""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 (fixed https URL from env)
            status = resp.status
            content_type = resp.headers.get("content-type", "")
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status = e.code
        content_type = e.headers.get("content-type", "") if e.headers else ""
        text = e.read().decode("utf-8", errors="replace")
    except TimeoutError:
        print("runtime timeout")
        return _reply(504, {"error": f"the agent didn't answer within {timeout:.0f} s"})
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            print("runtime timeout")
            return _reply(504, {"error": f"the agent didn't answer within {timeout:.0f} s"})
        print("runtime unreachable")
        return _reply(502, {"error": "couldn't reach the agent"})
    elapsed_ms = int((time.monotonic() - started) * 1000)

    # Step 5: log only the status and the time, never the token, prompt or reply.
    print(f"runtime status={status} elapsed_ms={elapsed_ms}")
    out: dict[str, Any] = {"status": status, "elapsed_ms": elapsed_ms}
    if status < 400:
        out["result"] = _parse_body(text, content_type)
    else:
        out["error"] = text[:2000]
    return _reply(200, out)
