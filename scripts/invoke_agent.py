"""Talk to the deployed agent on AgentCore Runtime.

    .venv/Scripts/python.exe scripts/invoke_agent.py --user alice --prompt "Where is order 1042?"
    .venv/Scripts/python.exe scripts/invoke_agent.py --user bob          # interactive
    .venv/Scripts/python.exe scripts/invoke_agent.py --sigv4 --prompt ".."  # runtime without a JWT authorizer

With --user (default alice) the script signs in to Cognito as that demo user (USER_PASSWORD_AUTH,
credentials from .deploy/<env>/users.json) and calls the Runtime over HTTPS with the user's Bearer token.
A runtime with a JWT authorizer does not accept SigV4; --sigv4 uses boto3 `invoke_agent_runtime`.

The runtime session id (>= 33 chars) is kept per user in .deploy/<env>/session-<user>.txt so a conversation
continues across runs; --new-session (or typing /new in interactive mode) starts a fresh one.
"""

# What this file does
#   Lets you chat with the agent that is deployed in AWS, signed in as one of the demo users
#   (alice, bob or carol). (The help text above, inside the triple quotes, is also what `--help`
#   prints, so the longer explanation lives here.)
#
# Where it fits
#   Run it after a Terraform deployment, which writes .deploy/<env>/outputs.json (addresses
#   and ids of what was deployed) and .deploy/<env>/users.json (the demo users' passwords).
#   `scripts/demo_access.py` reuses the functions in this file for its automated scenarios.
#
# The sign-in flow, step by step
#   1. Read .deploy/<env>/outputs.json to find the region, the agent's ARN and the Cognito app client id.
#   2. Sign in to Amazon Cognito with the user's name and password (from .deploy/<env>/users.json).
#      Cognito answers with an access token: a signed, short-lived "pass" that says who the user is
#      and which groups they belong to (alice: Group-ShopAssist-Admin, bob: Group-ShopAssist-Viewer, carol: none).
#   3. Send the message to the agent with an HTTPS POST to the Runtime's address, with the token in
#      the "Authorization: Bearer <token>" header and a session id in another header.
#   4. The Runtime checks the token before any agent code runs. carol has no allowed group, so she
#      gets HTTP 401/403 ("access denied"). For alice and bob the agent runs, and it passes the same
#      token on to the Gateway, which decides which tools that user may use.
#   5. Print the reply and a dim line with the skills and tools the agent used.
#
#   The session id is saved per user, so the next run continues the same conversation (the Runtime
#   keeps one small virtual machine, with the agent's memory, per session).
#
# Key terms
#   Amazon Cognito: the AWS service that stores users and passwords and issues login tokens.
#   Access token / JWT (JSON Web Token): the signed login token. "Bearer" means "whoever holds this
#       token may use it", so it is sent with every request.
#   ARN (Amazon Resource Name): the unique id of an AWS resource, here the agent runtime.
#   SigV4: AWS's own request-signing method that uses IAM (AWS account) credentials instead of a user
#       token. Only used with --sigv4, for a runtime that has no user sign-in (JWT authorizer).
#   Session id: a random id (at least 33 characters) that groups messages into one conversation.
#   HTTP 401/403: the standard web answers for "not signed in" and "not allowed".

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
# Folder written by the deployment of one environment: .deploy/<env>/ with outputs.json, users.json
# and the saved session ids. The environment comes from SHOPASSIST_ENV (default prod). The folder is
# git-ignored because users.json holds passwords.
ENVIRONMENT = os.environ.get("SHOPASSIST_ENV", "prod")
DEPLOY = ROOT / ".deploy" / ENVIRONMENT
# Terminal codes that print text dimmed (grey) and then switch back to normal.
DIM, RESET = "\033[2m", "\033[0m"
# The HTTP header the AgentCore Runtime reads to know which conversation (session) a message belongs to.
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"


class AccessDenied(Exception):
    """The Runtime refused the call (HTTP 401 or 403): the user may not use this agent.

    A separate error type lets callers (and demo scenario c) tell "access denied" apart from other
    failures.
    """

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


def load_json(path: Path) -> dict[str, Any]:
    """Read a JSON file from .deploy/, or stop with a hint to deploy first if it is missing."""
    if not path.exists():
        raise SystemExit(f"{path} not found. Deploy first (Terraform) or pass the values as flags.")
    return json.loads(path.read_text(encoding="utf-8"))


def new_session_id() -> str:
    """A new random session id, e.g. "session-3f2a..." (40 characters; the Runtime needs 33 or more)."""
    return f"session-{uuid.uuid4().hex}"  # 40 chars


def session_id_for(name: str, new: bool = False) -> str:
    """The saved session id for this user (to continue the conversation), or a new one.

    A new id is made and saved when asked for (`new=True`), when none is saved yet, or when the saved
    one is too short to be valid.
    """
    path = DEPLOY / f"session-{name}.txt"
    if not new and path.exists():
        sid = path.read_text(encoding="utf-8").strip()
        if len(sid) >= 33:
            return sid
    sid = new_session_id()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sid, encoding="utf-8")
    return sid


def user_access_token(region: str, client_id: str, username: str, password: str) -> str:
    """Sign in to Cognito with a username and password and return the user's access token."""
    import boto3

    # boto3 Cognito call "InitiateAuth" with the USER_PASSWORD_AUTH flow: send the username and
    # password to the user pool's app client and get tokens back. The app client is "public" (it has
    # no client secret), so no extra secret is needed here.
    cognito = boto3.client("cognito-idp", region_name=region)
    resp = cognito.initiate_auth(
        AuthFlow="USER_PASSWORD_AUTH",
        ClientId=client_id,
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    # If Cognito wants an extra step instead (a "challenge", such as "set a new password"), there are
    # no tokens. The deployment sets permanent passwords, so this should not happen for the demo users.
    result = resp.get("AuthenticationResult")
    if not result:
        raise SystemExit(f"Cognito sign-in for {username} needs a challenge ({resp.get('ChallengeName')}); not supported.")
    # The access token carries the user's groups (claim "cognito:groups"), which the Runtime and the
    # Gateway policy check.
    return result["AccessToken"]


def token_for_user(user: str, outputs: dict[str, Any], users_path: Path = DEPLOY / "users.json") -> str:
    """Look up a demo user's password in users.json and sign them in (steps 1 and 2 of the flow)."""
    users = load_json(users_path)
    if user not in users:
        raise SystemExit(f"unknown user {user!r}; known: {', '.join(users)}")
    creds = users[user]
    # The id of the Cognito app client for people (as opposed to the machine-to-machine client).
    client_id = outputs.get("cognito_users_client_id")
    if not client_id:
        raise SystemExit("outputs.json has no cognito_users_client_id")
    return user_access_token(outputs["region"], client_id, creds.get("username", user), creds["password"])


def runtime_url(region: str, arn: str, qualifier: str = "DEFAULT") -> str:
    """The HTTPS address that runs the agent: .../runtimes/<agent ARN>/invocations?qualifier=DEFAULT.

    The ARN contains ":" and "/", so it is URL-encoded to fit in the address. "DEFAULT" is the
    runtime endpoint (version) to call.
    """
    return (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{quote(arn, safe='')}/invocations"
        f"?qualifier={qualifier}"
    )


def _parse_body(text: str, content_type: str) -> dict[str, Any]:
    """Turn the Runtime's answer into a dict like {"response": ..., "skills_used": [...], "tools_used": [...]}.

    The answer may come as plain JSON or as a stream of "data:" lines (text/event-stream), and the
    JSON may be wrapped in a string. Anything that is not JSON is returned as the response text.
    """
    # Step 1: if it is a stream, glue the "data:" lines back together.
    if "text/event-stream" in content_type:
        chunks = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = "".join(chunks)
    # Step 2: read it as JSON (if that fails, the text itself is the reply).
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"response": text}
    # Step 3: unwrap JSON that was sent as a string inside JSON.
    if isinstance(data, str):  # streamed or double-encoded JSON
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return {"response": data}
    return data if isinstance(data, dict) else {"response": str(data)}


def invoke_https(url: str, token: str, session_id: str, prompt: str, timeout: float = 300) -> dict[str, Any]:
    """Send one message to the agent with the user's token (step 3 of the flow) and return the reply.

    Raises AccessDenied for HTTP 401/403 (the user may not use the agent) and RuntimeError for other
    errors. The timeout is generous (300 seconds) because one answer can involve several model and
    tool calls, and the first call of a new session also has to start the agent.
    """
    import httpx

    # Step 1: POST {"prompt": ...} to the Runtime. The headers carry the user's token (who is asking),
    # the content types, and the session id (which conversation this belongs to).
    resp = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            SESSION_HEADER: session_id,
        },
        content=json.dumps({"prompt": prompt}),
        timeout=timeout,
    )
    # Step 2: 401/403 means the Runtime's sign-in check (JWT authorizer) rejected this user.
    if resp.status_code in (401, 403):
        raise AccessDenied(resp.status_code, resp.text)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")
    # Step 3: read the agent's reply.
    return _parse_body(resp.text, resp.headers.get("content-type", ""))


def invoke_sigv4(region: str, arn: str, session_id: str, prompt: str) -> dict[str, Any]:
    """Send one message with AWS (IAM) credentials instead of a user token (the --sigv4 option).

    Only works for a runtime without a JWT authorizer; the ShopAssist agent runtime has one, so the
    normal path is `invoke_https`.
    """
    import boto3

    # boto3 call "InvokeAgentRuntime": the AWS SDK signs the request with your AWS credentials (SigV4).
    client = boto3.client("bedrock-agentcore", region_name=region)
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        qualifier="DEFAULT",
        runtimeSessionId=session_id,
        contentType="application/json",
        accept="application/json",
        payload=json.dumps({"prompt": prompt}).encode("utf-8"),
    )
    # The reply arrives as a stream of bytes; read it all, then parse it like the HTTPS reply.
    body = resp["response"].read().decode("utf-8")
    return _parse_body(body, resp.get("contentType", ""))


def print_reply(result: dict[str, Any]) -> None:
    """Print the agent's answer, then a dim line with the skills and tools it used ("-" if none)."""
    print(f"\n{result.get('response', '')}\n")
    skills = ", ".join(result.get("skills_used") or []) or "-"
    tools = ", ".join(result.get("tools_used") or []) or "-"
    print(f"{DIM}skills: {skills} | tools: {tools}{RESET}\n")


def main() -> int:
    """Sign in, then send one prompt or chat interactively.

    Exit codes: 0 = finished normally, 3 = access denied (the user may not use the agent).
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prompt", help="send one prompt and exit")
    parser.add_argument("--user", default="alice", help="demo user from .deploy/<env>/users.json (default alice)")
    parser.add_argument("--sigv4", action="store_true", help="use SigV4 (boto3) instead of a user token")
    parser.add_argument("--arn", help="agent runtime ARN (default: .deploy/<env>/outputs.json)")
    parser.add_argument("--region", help="AWS region (default: .deploy/<env>/outputs.json)")
    parser.add_argument("--outputs", default=str(DEPLOY / "outputs.json"))
    parser.add_argument("--new-session", action="store_true", help="start a new runtime session")
    args = parser.parse_args()

    # Step 1: find out where the agent is. outputs.json is skipped only when everything needed for a
    # SigV4 call was given on the command line.
    outputs: dict[str, Any] = {}
    if not (args.arn and args.region and args.sigv4):
        outputs = load_json(Path(args.outputs))
    arn = args.arn or outputs["agent_runtime_arn"]
    region = args.region or outputs["region"]
    outputs.setdefault("region", region)
    # Step 2: pick the conversation (session) to use for this user.
    who = "sigv4" if args.sigv4 else args.user
    session_id = session_id_for(who, new=args.new_session)

    # Step 3: sign in once at the start (no token in SigV4 mode).
    token = None if args.sigv4 else token_for_user(args.user, outputs)
    url = runtime_url(region, arn)

    def send(prompt: str) -> None:
        """Send one message (by HTTPS with the token, or SigV4) and print the reply or the denial."""
        try:
            result = invoke_sigv4(region, arn, session_id, prompt) if token is None else invoke_https(
                url, token, session_id, prompt
            )
        except AccessDenied as e:
            print(f"\nAccess denied (HTTP {e.status}): user '{who}' is not allowed to use this agent.")
            print(f"{DIM}{e.body[:300]}{RESET}\n")
            raise
        print_reply(result)

    print(f"{DIM}user={who} session={session_id}{RESET}")
    # Step 4a: one-shot mode (--prompt): send one message and stop.
    if args.prompt:
        try:
            send(args.prompt)
        except AccessDenied:
            return 3
        return 0

    # Step 4b: interactive mode: keep reading messages until the user quits.
    print("Type your message (/new for a new session, Ctrl+C or 'exit' to quit).")
    while True:
        try:
            prompt = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not prompt:
            continue
        if prompt.lower() in {"exit", "quit"}:
            return 0
        # "/new" starts a fresh conversation: the agent forgets what was said before.
        if prompt == "/new":
            session_id = session_id_for(who, new=True)
            print(f"{DIM}new session {session_id}{RESET}")
            continue
        # Access denied ends the chat (it will not change by retrying); other errors are shown and
        # the chat continues.
        try:
            send(prompt)
        except AccessDenied:
            return 3
        except Exception as e:  # noqa: BLE001
            print(f"error: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
