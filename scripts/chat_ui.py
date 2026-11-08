"""Local web chat for the deployed agent, with a JWT you paste in yourself.

    .venv/Scripts/python.exe scripts/chat_ui.py                       # http://127.0.0.1:8790
    .venv/Scripts/python.exe scripts/chat_ui.py --open                # and open the browser
    .venv/Scripts/python.exe scripts/chat_ui.py --print-token alice   # print a demo user's access token
    .venv/Scripts/python.exe scripts/chat_ui.py --local               # agent/app.py running locally
    .venv/Scripts/python.exe scripts/chat_ui.py --url https://...     # any other /invocations URL

The page (scripts/chat_ui.html) cannot call the AgentCore Runtime directly from the browser (the
Runtime does not answer CORS preflights), so this script serves the page and relays each message to
the Runtime with the pasted token, exactly as scripts/invoke_agent.py does.

--local targets http://localhost:8080/invocations: `agent/app.py` started on your machine with
GATEWAY_AUTH_MODE=none against scripts/local_gateway.py (see docs/11-chat-ui.md). Nothing checks the
token there, so the page offers dummy tokens; the role comes from `local_gateway.py --role`.
"""

# What this file does
#   A tiny local web server (Python standard library + httpx) with three routes:
#     GET  /             the chat page (scripts/chat_ui.html)
#     GET  /api/config   non-secret deployment info for the page (region, runtime ARN, users client id)
#     POST /api/invoke   {"token", "session_id", "prompt"} -> POST to the Runtime -> {"status", "result"}
#     GET  /login?idp=…  start a browser sign-in (code + PKCE) on Cognito's page (EntraID or COGNITO)
#     GET  /callback     Cognito's redirect back; exchanges the code, then hands the token to the page
#     POST /api/claim    {"id"} -> the signed-in token, exactly once (scripts/oidc_login.py)
#
# Security choices
#   - Listens on 127.0.0.1 only, and rejects requests whose Host header isn't this server (guards
#     against DNS rebinding) or whose Origin is another site.
#   - The target URL is fixed at start-up (outputs.json or --url). The browser can't choose where the
#     token is sent, so the relay can't be abused to send tokens elsewhere.
#   - Tokens are never logged or stored. They live in the page's memory only.
#   - Inputs are size-limited and checked (token charset, session id format, prompt length).

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from invoke_agent import (  # noqa: E402  (shared helpers, same contract as the CLI)
    DEPLOY,
    SESSION_HEADER,
    _parse_body,
    load_json,
    runtime_url,
    token_for_user,
)

PAGE = Path(__file__).resolve().parent / "chat_ui.html"
LOCAL_URL = "http://localhost:8080/invocations"  # `python agent/app.py` (BedrockAgentCoreApp, port 8080)
MAX_BODY = 64 * 1024
MAX_PROMPT = 4000
JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{33,100}$")
CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class Handler(BaseHTTPRequestHandler):
    server_version = "ShopAssistChatUI/1.0"
    # Set on the class by main().
    target_url: str = ""
    config: dict[str, Any] = {}
    allowed_hosts: set[str] = set()
    timeout: float = 300.0
    outputs: dict[str, Any] = {}
    port: int = 8790
    # Sign-ins in progress (state -> PendingLogin) and finished ones waiting for the page (id -> token).
    # Kept in memory only, each used once; the token never appears in a URL or a log.
    pending: dict[str, Any] = {}
    claims: dict[str, str] = {}
    lock = threading.Lock()

    # -- helpers ------------------------------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, status: int, data: dict[str, Any]) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _request_ok(self) -> bool:
        """Only accept requests addressed to this server, from this server's own page."""
        if self.headers.get("Host", "") not in self.allowed_hosts:
            self._json(421, {"error": "unexpected Host header"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin.removeprefix("http://") not in self.allowed_hosts:
            self._json(403, {"error": "cross-origin request refused"})
            return False
        return True

    def log_message(self, fmt: str, *args: Any) -> None:  # request line only; bodies (tokens) never logged
        # The /callback query string holds the one-time code: log the path only.
        line = (fmt % args).split("?", 1)[0]
        sys.stderr.write(f"{self.address_string()} {line}\n")

    # -- routes -------------------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if not self._request_ok():
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/config":
            self._json(200, self.config)
        elif path == "/login":
            self._start_login()
        elif path == "/callback":
            self._finish_login()
        else:
            self._json(404, {"error": "not found"})

    # -- browser sign-in ------------------------------------------------------------------------
    def _start_login(self) -> None:
        """Send the browser to Cognito's sign-in page (with Entra ID, or Cognito's own login)."""
        from oidc_login import PendingLogin

        if not self.outputs.get("cognito_domain_url"):
            self._json(400, {"error": "sign-in needs a deployed stack (outputs.json with cognito_domain_url)"})
            return
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        idp = query.get("idp", ["EntraID"])[0]
        if idp not in ("EntraID", "COGNITO"):
            self._json(400, {"error": "idp must be EntraID or COGNITO"})
            return
        login = PendingLogin(self.outputs, f"http://localhost:{self.port}/callback", None if idp == "COGNITO" else idp)
        with self.lock:
            self.pending[login.state] = login
        self._redirect(login.url)

    def _finish_login(self) -> None:
        """Cognito's redirect: check state, exchange the code, park the token for the page (one use)."""
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        with self.lock:
            login = self.pending.pop(query.get("state", [""])[0], None)
        if login is None:
            self._redirect("/#login_error=" + urllib.parse.quote("unknown or expired sign-in, try again"))
            return
        try:
            tokens = login.finish(query)
        except Exception as e:
            self._redirect("/#login_error=" + urllib.parse.quote(str(e)[:200]))
            return
        claim_id = secrets.token_urlsafe(24)
        with self.lock:
            self.claims[claim_id] = tokens["access_token"]
        self._redirect(f"/#login={claim_id}")

    def _claim(self, req: dict[str, Any]) -> None:
        with self.lock:
            token = self.claims.pop(str(req.get("id", "")), None)
        if token is None:
            self._json(404, {"error": "nothing to claim (already used or expired)"})
        else:
            self._json(200, {"token": token})

    def do_POST(self) -> None:  # noqa: N802
        if not self._request_ok():
            return
        if self.path not in ("/api/invoke", "/api/claim"):
            self._json(404, {"error": "not found"})
            return
        if "application/json" not in self.headers.get("Content-Type", ""):
            self._json(415, {"error": "Content-Type must be application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY:
            self._json(413, {"error": f"body must be 1..{MAX_BODY} bytes"})
            return
        try:
            req = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._json(400, {"error": "body is not JSON"})
            return

        if self.path == "/api/claim":
            self._claim(req)
            return

        token = str(req.get("token", "")).strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        session_id = str(req.get("session_id", ""))
        prompt = req.get("prompt")
        if not JWT_RE.match(token) or len(token) > 8192:
            self._json(400, {"error": "token doesn't look like a JWT (header.payload.signature)"})
            return
        if not SESSION_RE.match(session_id):
            self._json(400, {"error": "session_id must be 33-100 chars of letters, digits, '-' or '_'"})
            return
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
            self._json(400, {"error": f"prompt must be 1..{MAX_PROMPT} characters"})
            return

        import httpx

        started = time.monotonic()
        try:
            resp = httpx.post(
                self.target_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    SESSION_HEADER: session_id,
                },
                content=json.dumps({"prompt": prompt}),
                timeout=self.timeout,
            )
        except httpx.TimeoutException:
            self._json(504, {"error": f"the agent didn't answer within {self.timeout:.0f} s"})
            return
        except httpx.HTTPError as e:
            self._json(502, {"error": f"couldn't reach the agent: {e.__class__.__name__}: {e}"})
            return

        elapsed_ms = int((time.monotonic() - started) * 1000)
        out: dict[str, Any] = {"status": resp.status_code, "elapsed_ms": elapsed_ms}
        if resp.status_code < 400:
            out["result"] = _parse_body(resp.text, resp.headers.get("content-type", ""))
        else:
            out["error"] = resp.text[:2000]
        self._json(200, out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8790, help="local port (default 8790)")
    parser.add_argument("--outputs", default=str(DEPLOY / "outputs.json"))
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument("--local", nargs="?", const=LOCAL_URL, metavar="URL",
                              help=f"talk to the agent running locally (default {LOCAL_URL}); no outputs.json needed")
    target_group.add_argument("--url", help="send messages here instead of the Runtime in outputs.json")
    parser.add_argument("--timeout", type=float, default=300.0, help="per-message timeout in seconds")
    parser.add_argument("--open", action="store_true", help="open the page in the default browser")
    parser.add_argument("--print-token", metavar="USER",
                        help="sign in as a demo user from .deploy/<env>/users.json, print the access token, exit")
    args = parser.parse_args()

    custom = args.local or args.url
    outputs: dict[str, Any] = {}
    if not custom or args.print_token:
        outputs = load_json(Path(args.outputs))

    if args.print_token:
        print(token_for_user(args.print_token, outputs))
        return 0

    if custom:
        if not re.match(r"^https?://", custom):
            raise SystemExit("--local/--url must start with http:// or https://")
        target = custom
    else:
        target = runtime_url(outputs["region"], outputs["agent_runtime_arn"])
    mode = "local" if args.local else "custom" if args.url else "agentcore-runtime"

    Handler.target_url = target
    Handler.timeout = args.timeout
    Handler.allowed_hosts = {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}
    Handler.outputs = outputs
    Handler.port = args.port
    Handler.config = {
        "target": mode,
        "target_url": target,
        "region": outputs.get("region"),
        "agent_runtime_arn": outputs.get("agent_runtime_arn"),
        "cognito_users_client_id": outputs.get("cognito_users_client_id"),
        "deployed_by": outputs.get("deployed_by"),
        # Browser sign-in works only on port 8790 (the callback address registered in Cognito).
        "login_available": bool(outputs.get("cognito_domain_url")) and args.port == 8790,
        "entra_enabled": bool(outputs.get("entra_enabled")),
    }

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    # localhost (not 127.0.0.1): it must match the sign-in callback address registered in Cognito.
    url = f"http://localhost:{args.port}/"
    print(f"ShopAssist chat UI on {url}  ->  {target}")
    print("Ctrl+C to stop.")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
