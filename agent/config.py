"""Runtime configuration for the generic agent, read from environment variables.

What this file does
    Collects every setting the agent needs (which AI model to use, where the Gateway is, how to log in to it,
    where the skills live) into one read-only `Settings` object. Each value comes from an environment
    variable, or falls back to a sensible default.

Where it fits
    `app.py` calls `Settings.from_env()` once when the agent starts. The other modules (`gateway_auth.py`,
    `skill_source.py`) read their values from that object. The variable names match
    `.kiro/specs/strands-poc/design.md` section 3. In the cloud, Terraform or the boto3 scripts set these
    variables on the AgentCore Runtime. Locally, `scripts/run_local.py` sets them.

Step by step
    1. Read each environment variable (for example `MODEL_ID`, `GATEWAY_URL`, `GATEWAY_AUTH_MODE`).
    2. Check that `GATEWAY_AUTH_MODE` is one of the three allowed modes, and stop with a clear error if not.
    3. Fill in a default for anything that is not set.
    4. Return a frozen (unchangeable) `Settings` object.

    Nothing here is specific to the ShopAssist store: every value is a connection detail or a source path.

Key terms
    Environment variable: a named setting given to a program from outside, so the code needs no edits per
        environment.
    Gateway: Amazon Bedrock AgentCore Gateway, the service that exposes the store's tools (orders, support) to
        the agent over MCP.
    MCP (Model Context Protocol): a standard way for an AI agent to discover and call tools.
    Auth mode: how the agent proves who it is to the Gateway (see `gateway_auth.py`).
    S3: Amazon's file storage service. The skills can be stored there instead of inside the agent package.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# The three ways the agent can log in to the Gateway:
# - "passthrough": forward the end user's own login token (the normal, deployed mode; enables per-user access).
# - "client_credentials": the agent logs in as itself with a client id and secret (handy for local development).
# - "none": send no login at all (only for a local test server that doesn't check logins).
AUTH_MODES = ("passthrough", "client_credentials", "none")

# Defaults used when the matching environment variable is not set.
DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"  # Claude Haiku on Amazon Bedrock
DEFAULT_REGION = "us-east-1"  # AWS region (data centre location) the agent talks to
DEFAULT_GATEWAY_URL = "http://localhost:8000/mcp"  # a local MCP server, for development only


def _default_skills_cache_dir() -> str:
    """Pick the folder where skills downloaded from S3 are stored on this machine.

    The skills folder is wiped and refilled on each download, so it must be a writable scratch folder.
    """
    # The Runtime is Linux, where /tmp is writable. Elsewhere (local dev on Windows) use the OS temp dir.
    if os.name == "posix":
        return "/tmp/skills"
    return str(Path(tempfile.gettempdir()) / "skills")


@dataclass(frozen=True)
class Settings:
    """All agent settings in one place. "frozen" means the values can't be changed after start-up.

    Fields (with the environment variable that sets each one):
        model_id: Bedrock model the agent thinks with (`MODEL_ID`).
        region: AWS region for Bedrock (`AWS_REGION`, or `AWS_DEFAULT_REGION`).
        gateway_url: web address of the Gateway's MCP endpoint (`GATEWAY_URL`).
        gateway_auth_mode: one of `AUTH_MODES` above (`GATEWAY_AUTH_MODE`).
        oauth_provider_name: name of the AgentCore Identity login provider (`OAUTH_PROVIDER_NAME`).
            Kept for the design contract; this module set does not read it in the current code.
        oauth_scopes: permissions requested when the agent logs in as itself (`OAUTH_SCOPES`, space separated).
        cognito_token_url / cognito_client_id / cognito_client_secret: login details for
            "client_credentials" mode (`COGNITO_TOKEN_URL`, `COGNITO_CLIENT_ID`, `COGNITO_CLIENT_SECRET`).
            The secret is hidden when the settings are printed (`repr=False`) so it doesn't leak into logs.
        skills_s3_uri: if set, download the skills from this S3 location (`SKILLS_S3_URI`).
        skills_dir: otherwise, read the skills from this local folder (`SKILLS_DIR`).
        skills_cache_dir: where S3 skills are downloaded to (`SKILLS_CACHE_DIR`).
        mcp_startup_timeout: seconds to wait for the Gateway connection to open (`MCP_STARTUP_TIMEOUT`).
    """

    model_id: str = DEFAULT_MODEL_ID
    region: str = DEFAULT_REGION
    gateway_url: str = DEFAULT_GATEWAY_URL
    gateway_auth_mode: str = "passthrough"
    oauth_provider_name: str = "shopassist-cognito"
    oauth_scopes: tuple[str, ...] = ("shopassist/invoke",)
    cognito_token_url: str = ""
    cognito_client_id: str = ""
    cognito_client_secret: str = field(default="", repr=False)
    skills_s3_uri: str = ""
    skills_dir: str = "./skills"
    skills_cache_dir: str = field(default_factory=_default_skills_cache_dir)
    mcp_startup_timeout: int = 30

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        """Build the settings from environment variables.

        Tests can pass their own `env` dictionary instead of the real environment.
        """
        e = os.environ if env is None else env
        # Step 1: validate the auth mode first, so a typo fails fast with a clear message
        # instead of causing confusing login errors later.
        mode = (e.get("GATEWAY_AUTH_MODE") or "passthrough").strip().lower()
        if mode not in AUTH_MODES:
            raise ValueError(f"GATEWAY_AUTH_MODE must be one of {AUTH_MODES}, got {mode!r}")
        # Step 2: read every other value, using the default when a variable is missing or empty.
        return cls(
            model_id=e.get("MODEL_ID") or DEFAULT_MODEL_ID,
            region=e.get("AWS_REGION") or e.get("AWS_DEFAULT_REGION") or DEFAULT_REGION,
            gateway_url=e.get("GATEWAY_URL") or DEFAULT_GATEWAY_URL,
            gateway_auth_mode=mode,
            oauth_provider_name=e.get("OAUTH_PROVIDER_NAME") or "shopassist-cognito",
            oauth_scopes=tuple((e.get("OAUTH_SCOPES") or "shopassist/invoke").split()),
            cognito_token_url=e.get("COGNITO_TOKEN_URL", ""),
            cognito_client_id=e.get("COGNITO_CLIENT_ID", ""),
            cognito_client_secret=e.get("COGNITO_CLIENT_SECRET", ""),
            skills_s3_uri=e.get("SKILLS_S3_URI", ""),
            skills_dir=e.get("SKILLS_DIR") or "./skills",
            skills_cache_dir=e.get("SKILLS_CACHE_DIR") or _default_skills_cache_dir(),
            mcp_startup_timeout=int(e.get("MCP_STARTUP_TIMEOUT") or 30),
        )
