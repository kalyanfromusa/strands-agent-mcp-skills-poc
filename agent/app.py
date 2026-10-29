"""Generic skills-driven agent hosted on Amazon Bedrock AgentCore Runtime.

What this file does
    This is the agent's main program. It receives a customer's message, builds (or reuses) an AI agent for
    that customer, lets the agent work out the answer with skills and tools, and sends back the reply plus
    the list of skills and tools it used.

    The agent has no domain knowledge. Its behaviour comes from skills (SKILL.md folders,
    loaded with the Strands `AgentSkills` plugin) and its data and rules from MCP tools behind
    the Gateway. Entry point for the Runtime: `app.py` at the zip root (`python app.py`).

    In plain words: this code knows nothing about orders, returns or tickets. Swap the skills folder and the
    Gateway tools, and the same code becomes a different assistant. That is the "generic agent" idea.

Where it fits
    Caller (`scripts/invoke_agent.py` or `scripts/run_local.py`) -> AgentCore Runtime -> `invoke()` below ->
    Strands agent -> Claude on Amazon Bedrock (thinking) and AgentCore Gateway (tools: `orders` Lambda,
    `support` MCP server) -> reply back to the caller.
    It uses `config.py` (settings), `skill_source.py` (where skills are), `skill_binding.py` (which tools a
    skill may use), `gateway_auth.py` (login token for the Gateway) and `tools.py` (`read_skill_resource`).

Step by step (one request, from message to reply)
    1. The Runtime receives `{"prompt": "..."}` with the user's `Authorization` header and calls `invoke()`.
    2. `invoke()` checks the message and reads the `Authorization` header (the user's login token).
    3. `AgentRuntime.invoke()` stores the token so every Gateway call carries it (passthrough mode).
    4. It finds the agent for this user, or builds a new one if this is a different user (per-user caching):
       a. load the skills (once), b. connect to the Gateway and fetch the tools this user may see,
       c. create the Strands agent with the model, the system prompt, the tools and the plugins,
       d. check the skills against the tools and log any mismatch.
    5. The agent runs. "Progressive disclosure" keeps the prompt small:
       - the model first sees only each skill's name and description (added to the system prompt);
       - it activates the best-matching skill with the `skills` tool and gets the full SKILL.md text, plus
         the "[Tool binding]" list of exact tool names this user can use (from `skill_binding.py`);
       - if needed it reads extra files with `read_skill_resource`;
       - it calls Gateway tools such as `orders___get_order`; calls to servers no active skill declares
         are blocked.
    6. The reply text, `skills_used` and `tools_used` are returned to the caller. Errors are returned as an
       "Error: ..." reply instead of crashing the service.

Key terms
    Amazon Bedrock AgentCore Runtime: the AWS service that hosts and runs this agent code in small, isolated
        virtual machines (microVMs), one per conversation session.
    Strands Agents: the open-source Python library that runs the "think, call a tool, think again" loop.
    Skill: a folder with a SKILL.md file describing how to handle one kind of request.
    MCP (Model Context Protocol): a standard way for an AI agent to discover and call tools.
    Gateway: AgentCore Gateway, which offers the store's tools over MCP and checks who may call them.
    System prompt: fixed instructions the model reads before every conversation.
    Bearer token / JWT: the user's signed login token; its `sub` claim is the user's unique id.
    Passthrough: forwarding the user's own token to the Gateway, so it can apply per-user permissions.

Import layout: modules are flat (`from config import Settings`), so the zip root holds
`app.py`, `config.py`, ... next to the dependencies. Locally, `agent/` is put on sys.path.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

# Make the sibling modules (config.py, gateway_auth.py, ...) importable both locally and in the deployed zip.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from bedrock_agentcore.runtime import BedrockAgentCoreApp  # noqa: E402

from config import Settings  # noqa: E402
from gateway_auth import bearer_from_header, bind_request_token, build_gateway_auth, jwt_claims  # noqa: E402
from skill_binding import SkillBinding, gateway_tools_of, split_gateway_name, validate_skills  # noqa: E402

logger = logging.getLogger("agent")

# The fixed instructions the model reads first. They contain no store knowledge: they only tell the model
# to pick a skill, follow it, use only the tools it resolves to, and never invent data. The AgentSkills plugin
# adds the list of available skills (names and descriptions) to the end of this prompt before each request.
SYSTEM_PROMPT = """You are a helpful assistant.

How you work:
- Skills hold the procedures for every task. Before using any other tool, activate the skill whose description best matches the request with the `skills` tool, then follow its instructions step by step.
- If no skill matches, say briefly what you can help with (based on the available skills) instead of improvising.
- Only use the tools a skill tells you to use, with the exact tool names it resolves to. If a tool call is blocked or unavailable, follow the message you get back.
- Use `read_skill_resource` to read reference files a skill points you to.
- Never invent data. Every fact, identifier, amount or status you state must come from a tool result in this conversation. If you don't have it, ask the user or say you can't find it.
- Ask for missing details instead of guessing them. Ask for one or two things at a time.
- Keep replies short, friendly and plain. Don't mention internal tool names, skills or system details to the user."""


def should_rebuild(current_key: str | None, new_key: str, has_agent: bool) -> bool:
    """Rebuild the agent (and its Gateway connection) when there is none or the caller changed.

    Why rebuild for a new caller: the agent's tool list comes from the Gateway for one specific user (bob sees
    fewer tools than alice), and the agent remembers the conversation so far. Reusing it for another user
    would show them the wrong tools and someone else's conversation.
    """
    return not has_agent or current_key != new_key


def find_header(headers: dict[str, str] | None, name: str) -> str | None:
    """Look up a request header by name, ignoring upper/lower case ("authorization" = "Authorization")."""
    if not headers:
        return None
    lname = name.lower()
    for key, value in headers.items():
        if key.lower() == lname:
            return value
    return None


def caller_key(mode: str, token: str | None) -> str:
    """Cache key for the agent: the token's `sub` in passthrough mode, else the auth mode.

    `sub` is the user's unique id in Cognito, so alice and bob get different keys. In the other modes every
    request uses the same agent login, so one shared agent is enough.
    """
    if mode != "passthrough":
        return mode
    claims = jwt_claims(token)
    return str(claims.get("sub") or claims.get("client_id") or "unknown")


class AgentRuntime:
    """Builds the Strands agent lazily and keeps one per caller identity.

    "Lazily" means the agent is built on the first request, not at start-up. Only one agent is kept at a time:
    it is reused while the same user keeps calling, and replaced when a different user calls.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        """Read the settings and prepare the Gateway login. No agent is built yet."""
        self.settings = settings or Settings.from_env()
        # The lock makes requests take turns, so two requests never share or rebuild the agent at once.
        self._lock = threading.Lock()
        self._skills: list[Any] | None = None
        self._agent: Any = None
        self._binding: SkillBinding | None = None
        self._key: str | None = None
        self._auth = build_gateway_auth(self.settings)

    # -- construction --------------------------------------------------------------------------
    def skills(self) -> list[Any]:
        """Load the skills once (from S3 or the local folder) and keep them for all later requests.

        Note: skills are read only the first time. Updated skill files are picked up when the Runtime
        starts a new session (a new microVM), not in the middle of a running one.
        """
        if self._skills is None:
            from strands.vended_plugins.skills import Skill

            from skill_source import resolve_skills_dir

            skills_dir = resolve_skills_dir(self.settings)
            self._skills = Skill.from_directory(skills_dir)
            logger.info("loaded %d skills from %s: %s", len(self._skills), skills_dir, [s.name for s in self._skills])
        return self._skills

    def _build(self) -> tuple[Any, SkillBinding]:
        """Create a new Strands agent for the current caller, connected to the Gateway.

        Returns the agent and its `SkillBinding` plugin (which records the skills and tools used).
        """
        from strands import Agent
        from strands.models.bedrock import BedrockModel
        from strands.tools.mcp import MCPClient
        from strands.vended_plugins.skills import AgentSkills

        from tools import make_read_skill_resource

        s = self.settings
        # Step 1: get the skills.
        skills = self.skills()
        # Step 2: prepare the connection to the Gateway. `auth_provider` adds the login token to every message.
        gateway = MCPClient(url=s.gateway_url, auth_provider=self._auth, startup_timeout=s.mcp_startup_timeout)
        # Step 3: create the skill-to-tool binding plugin (see skill_binding.py).
        binding = SkillBinding(skills)
        # Step 4: create the agent. Creating it opens the Gateway connection and asks for the tool list
        # (MCP "tools/list"). The Gateway answers with only the tools this user's policy allows.
        # - model: Claude on Bedrock, the "brain".
        # - tools: all Gateway tools, plus the local `read_skill_resource` tool for skill reference files.
        # - plugins: AgentSkills (adds the skill list to the prompt and the `skills` tool), and our binding.
        # - callback_handler=None: don't stream text to the console; we return the final reply instead.
        agent = Agent(
            model=BedrockModel(model_id=s.model_id, region_name=s.region),
            system_prompt=SYSTEM_PROMPT,
            tools=[gateway, make_read_skill_resource({sk.name: sk.path for sk in skills if sk.path})],
            plugins=[AgentSkills(skills=skills), binding],
            callback_handler=None,
        )
        # Step 5: log which Gateway tools this caller got, and which tools are local.
        gateway_tools = gateway_tools_of(agent)
        logger.info("gateway tools visible to this caller: %s", sorted(gateway_tools))
        local_tools = [n for n in agent.tool_registry.registry if not split_gateway_name(n)]
        # Per-user tool filtering is expected in passthrough mode, so missing tools are informational there.
        level = logging.INFO if s.gateway_auth_mode == "passthrough" else logging.WARNING
        # Step 6: check every skill against the tool list and log mismatches (it never stops the agent).
        validate_skills(skills, gateway_tools, local_tools, missing_level=level)
        return agent, binding

    def _agent_for(self, key: str) -> tuple[Any, SkillBinding]:
        """Return the agent for caller `key`, building a new one if the caller changed (per-user caching).

        Reusing the agent for the same user saves the time of reconnecting to the Gateway, and keeps the
        conversation history, so follow-up questions ("and the other order?") make sense.
        """
        if should_rebuild(self._key, key, self._agent is not None):
            if self._agent is not None:
                logger.info("caller changed, rebuilding agent and Gateway connection")
                # Close the old Gateway connection. A failure here is logged but doesn't stop the new user.
                try:
                    self._agent.cleanup()
                except Exception:  # noqa: BLE001
                    logger.exception("cleanup of previous agent failed")
                self._agent = self._binding = self._key = None
            self._agent, self._binding = self._build()
            self._key = key
        assert self._binding is not None
        return self._agent, self._binding

    # -- invocation ----------------------------------------------------------------------------
    def invoke(self, prompt: str, authorization: str | None = None) -> dict[str, Any]:
        """Answer one message. `authorization` is the raw `Authorization` header of the request.

        Returns `{"response": reply text, "skills_used": [...], "tools_used": [...]}`, plus `"error"` on failure.
        """
        # Step 1: pull the user's token out of the header.
        mode = self.settings.gateway_auth_mode
        token = bearer_from_header(authorization)
        # Step 2: in passthrough mode there is nothing to forward without a token, so stop with a clear message.
        # (This usually means the Runtime was not set up to forward the Authorization header.)
        if mode == "passthrough" and not token:
            return {
                "response": "Error: no Authorization header reached the agent, so it can't call its tools.",
                "skills_used": [],
                "tools_used": [],
                "error": "missing_authorization",
            }
        # Requests take turns (the lock), so the stored token always belongs to the request being answered.
        with self._lock:
            # Step 3: store this user's token so every Gateway message from now on carries it.
            # This happens before the agent is built, because building it already calls the Gateway.
            if mode == "passthrough":
                bind_request_token(token)
            # Step 4: get this user's agent (reuse it, or build a new one if the user changed).
            agent, binding = self._agent_for(caller_key(mode, token))
            # Step 5: run the agent. The model reads the prompt, activates a skill, calls tools, and writes
            # the reply. This can involve several model calls and tool calls.
            result = agent(prompt)
            # Step 6: return the reply and what was used (the CLI prints the usage as a dim line).
            return {
                "response": str(result).strip(),
                "skills_used": list(binding.skills_used),
                "tools_used": list(binding.tools_used),
            }


# The AgentCore SDK's small web server. It answers the Runtime's requests and calls the
# function marked with @app.entrypoint below.
app = BedrockAgentCoreApp()
# One AgentRuntime per process, created on first use (see get_runtime).
_runtime: AgentRuntime | None = None
_runtime_lock = threading.Lock()


def get_runtime() -> AgentRuntime:
    """Return the single shared AgentRuntime, creating it on the first request."""
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = AgentRuntime()
        return _runtime


def _authorization_from(context: Any) -> str | None:
    """Find the `Authorization` header of the current request.

    The Runtime only passes headers that are on its allowlist (here, `Authorization`). They normally arrive
    on `context.request_headers`; the SDK's per-request context is checked as a fallback.
    """
    value = find_header(getattr(context, "request_headers", None), "Authorization")
    if value:
        return value
    from bedrock_agentcore.runtime.context import BedrockAgentCoreContext

    return find_header(BedrockAgentCoreContext.get_request_headers(), "Authorization")


@app.entrypoint
def invoke(payload: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Entry point the AgentCore Runtime calls for every request.

    `payload` is the JSON body sent by the caller, e.g. `{"prompt": "Where is order 1042?"}`. `context` holds
    request details such as the session id and the forwarded headers.
    """
    # Step 1: check the request contains a non-empty prompt.
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        return {"response": "Error: payload must be {\"prompt\": \"...\"}.", "skills_used": [], "tools_used": []}
    logger.info("invocation session=%s", getattr(context, "session_id", None))
    # Step 2: answer it. Any unexpected failure is logged and returned as an "Error: ..." reply,
    # so the caller always gets an answer in the same shape.
    try:
        return get_runtime().invoke(prompt, _authorization_from(context))
    except Exception as e:  # noqa: BLE001
        logger.exception("invocation failed")
        return {"response": f"Error: {e}", "skills_used": [], "tools_used": [], "error": type(e).__name__}


# Start the web server when the file is run directly (`python app.py`), which is how the Runtime starts it.
# Port 8080 is the port AgentCore Runtime sends requests to.
if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    app.run(host=os.getenv("AGENT_HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8080")))
