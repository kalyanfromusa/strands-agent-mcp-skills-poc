"""Chat with the generic agent locally: local Gateway emulator + real Bedrock.

Start the emulator first (`scripts/local_gateway.py`), then:

    .venv/Scripts/python.exe scripts/run_local.py                 # interactive
    .venv/Scripts/python.exe scripts/run_local.py --prompt "..."  # one shot

Defaults: GATEWAY_AUTH_MODE=none, GATEWAY_URL=http://localhost:8000/mcp, SKILLS_DIR=./skills,
MODEL_ID=us.anthropic.claude-haiku-5-5, AWS_REGION=us-east-1. Environment variables override them
(e.g. GATEWAY_AUTH_MODE=client_credentials with COGNITO_* to talk to the real Gateway).
"""

# What this file does
#   Lets you chat with the ShopAssist agent on your own computer. The agent's brain (the Claude
#   model) is the real one on Amazon Bedrock, but its tools come from the local Gateway stand-in.
#   (The help text above, inside the triple quotes, is also what `--help` prints, so it is kept short.)
#
# Where it fits
#   Run `scripts/local_gateway.py` first (in another terminal), then this script. It loads the same
#   agent code that runs in AWS (agent/app.py) and prints each reply, then a dim line listing the
#   skills and tools the agent used. Good for trying prompts and skills before deploying anything.
#   You need AWS credentials that may call Bedrock; nothing else in AWS is used.
#
# Step by step
#   1. Read the options (--prompt, --gateway-url, --verbose).
#   2. Fill in local defaults for the agent's settings: no Gateway sign-in, the local Gateway
#      address, the local skills/ folder, the Claude Haiku model and region us-east-1. Settings you
#      have already set in your environment win over these defaults.
#   3. Prepare the agent (AgentRuntime). The agent is actually built on the first message: that is
#      when it loads the skills and connects to the Gateway to fetch the tool list.
#   4. Send one prompt (--prompt) or loop: read a message, ask the agent, print the reply.
#
# Key terms
#   Amazon Bedrock: the AWS service that hosts AI models such as Claude.
#   Gateway: the single address the agent uses to find and call tools (here, the local stand-in).
#   Skill: a SKILL.md file in skills/ that tells the agent the procedure for one kind of request.
#   Environment variable: a named setting a program reads at start-up (e.g. GATEWAY_URL).

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Make the agent code in agent/ importable, so we run exactly the code that is deployed.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

# Terminal codes that print text dimmed (grey) and then switch back to normal.
DIM, RESET = "\033[2m", "\033[0m"


def print_reply(result: dict) -> None:
    """Print the agent's answer, then a dim line with the skills and tools it used ("-" if none)."""
    print(f"\n{result.get('response', '')}\n")
    skills = ", ".join(result.get("skills_used") or []) or "-"
    tools = ", ".join(result.get("tools_used") or []) or "-"
    print(f"{DIM}skills: {skills} | tools: {tools}{RESET}\n")


def main() -> int:
    """Set up the local agent and chat with it. Returns 0 (success) when the user quits."""
    # Step 1: read the command-line options.
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prompt", help="send one prompt and exit")
    parser.add_argument("--gateway-url", help="override GATEWAY_URL")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    # Step 2: local defaults. setdefault only fills a setting that is not set yet, so your own
    # environment variables always win (e.g. to point at the real Gateway instead).
    os.environ.setdefault("GATEWAY_AUTH_MODE", "none")
    os.environ.setdefault("GATEWAY_URL", "http://localhost:8000/mcp")
    os.environ.setdefault("SKILLS_DIR", str(ROOT / "skills"))
    os.environ.setdefault("MODEL_ID", "us.anthropic.claude-haiku-5-5")
    os.environ.setdefault("AWS_REGION", "us-east-1")
    if args.gateway_url:
        os.environ["GATEWAY_URL"] = args.gateway_url

    # Quiet logs by default; --verbose shows debug detail (e.g. how skills are bound to tools).
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("agent").setLevel(logging.DEBUG if args.verbose else logging.INFO)
    logging.getLogger("skill_binding").setLevel(logging.DEBUG if args.verbose else logging.WARNING)

    # Step 3: import the agent only now, after the settings above are in place, then prepare it.
    from app import AgentRuntime  # noqa: E402
    from config import Settings  # noqa: E402

    settings = Settings.from_env()
    runtime = AgentRuntime(settings)
    print(f"{DIM}model={settings.model_id} gateway={settings.gateway_url} auth={settings.gateway_auth_mode}{RESET}")

    # Step 4a: one-shot mode: answer a single prompt and stop.
    if args.prompt:
        print_reply(runtime.invoke(args.prompt))
        return 0

    # Step 4b: interactive mode. The same agent object is reused, so it remembers the conversation.
    print("Type your message (Ctrl+C or 'exit' to quit).")
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
        # An error (e.g. the local Gateway is not running) is printed, and the chat carries on.
        try:
            print_reply(runtime.invoke(prompt))
        except Exception as e:  # noqa: BLE001
            print(f"error: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
