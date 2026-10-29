"""Generic local tools for the agent (no Gateway involved).

What this file does
    Provides the one tool the agent runs itself rather than through the Gateway: `read_skill_resource`. It lets
    the AI model open an extra reference file that belongs to a skill, for example
    `references/return-policy.md` in the `returns-and-refunds` skill.

Where it fits
    `app.py` creates this tool with `make_read_skill_resource()` when it builds the agent, next to the Gateway
    tools. The model calls it when an activated skill's instructions point to a reference file. This is the
    last level of "progressive disclosure": the model first sees only skill names and descriptions, then
    the full SKILL.md when it activates a skill, and only then the extra files it actually needs. This keeps
    the prompt small and cheap.

Step by step
    1. The model calls `read_skill_resource(skill_name, path)`.
    2. Check that the skill exists and has been activated in this conversation.
    3. Check that `path` stays inside that skill's folder (no `..`, no absolute paths, no hidden tricks).
    4. Check the file exists and is not too large (200,000 bytes at most).
    5. Return the file text, or a short "Error: ..." message the model can read and act on.

Key terms
    Tool: a function the AI model may ask the program to run, by name, with arguments.
    Skill: a folder with a `SKILL.md` file of instructions (and optional extra files) for one kind of task.
    Activated skill: a skill whose full instructions the model has loaded with the `skills` tool.
    Agent state: a small memory the Strands agent keeps between turns; the skills plugin stores the list of
        activated skills there under the key "agent_skills".
    Path traversal: a trick that uses `..` or an absolute path to read files outside the intended folder.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from strands import tool
from strands.types.tools import ToolContext

# Upper limit on a resource file, so one large file can't flood the model's prompt (and the cost).
MAX_RESOURCE_BYTES = 200_000
# Where the Strands AgentSkills plugin keeps its state (including "activated_skills") in agent.state.
SKILLS_STATE_KEY = "agent_skills"


class ResourceError(ValueError):
    """A skill resource request that cannot be served.

    The message is shown to the model (as "Error: ..."), so it is written to tell it what to do instead.
    """


def resolve_skill_resource(skill_dir: str | Path, path: str) -> Path:
    """Resolve `path` inside `skill_dir`, rejecting anything that escapes it.

    This is a security check: the model chooses `path`, so it must not be able to read any file on the
    machine, only files inside the skill's own folder.
    """
    # Step 1: a path is required, and a hidden "null" character (a classic attack trick) is refused.
    if not path or not path.strip():
        raise ResourceError("path is required, e.g. 'references/<file>.md'")
    raw = path.strip()
    if "\x00" in raw:
        raise ResourceError("invalid path")
    # Step 2: refuse absolute paths in both Linux style (/etc/...) and Windows style (C:\...).
    if PurePosixPath(raw).is_absolute() or PureWindowsPath(raw).is_absolute() or PureWindowsPath(raw).drive:
        raise ResourceError("path must be relative to the skill directory")
    # Step 3: build the full path, then confirm it still sits inside the skill folder. "resolve()" expands
    # any ".." parts first, so "references/../../secret" is caught here.
    base = Path(skill_dir).resolve()
    target = (base / raw.replace("\\", "/")).resolve()
    if target != base and base not in target.parents:
        raise ResourceError("path escapes the skill directory")
    # Step 4: it must be an existing file (not a folder).
    if not target.is_file():
        raise ResourceError(f"no such file in the skill: {path}")
    return target


def read_resource_text(skill_dirs: dict[str, Path], skill_name: str, path: str, activated: list[str] | None) -> str:
    """Return the text of a resource file of an activated skill.

    `activated` is the list of skills activated so far. `None` means "don't check" (used by tests).
    """
    # Step 1: the skill must exist.
    if skill_name not in skill_dirs:
        raise ResourceError(f"unknown skill '{skill_name}'. Known skills: {', '.join(sorted(skill_dirs))}")
    # Step 2: the skill must be active, so the model follows the normal order: activate, then read references.
    if activated is not None and skill_name not in activated:
        raise ResourceError(f"skill '{skill_name}' is not active. Activate it with the `skills` tool first.")
    # Step 3: the path must be safe and the file not too large.
    target = resolve_skill_resource(skill_dirs[skill_name], path)
    if target.stat().st_size > MAX_RESOURCE_BYTES:
        raise ResourceError(f"file is larger than {MAX_RESOURCE_BYTES} bytes")
    # Step 4: read it as text. Bad characters are replaced rather than crashing the tool.
    return target.read_text(encoding="utf-8", errors="replace")


def _activated_skills(agent: Any) -> list[str]:
    """Names of the skills the model has activated so far, read from the skills plugin's state."""
    state = agent.state.get(SKILLS_STATE_KEY) if agent is not None else None
    if isinstance(state, dict):
        return list(state.get("activated_skills", []))
    return []


def make_read_skill_resource(skill_dirs: dict[str, Path]) -> Any:
    """Build the `read_skill_resource` tool bound to the loaded skill directories.

    `skill_dirs` maps each skill name to its folder. The tool is created inside this function so it
    "remembers" those folders without needing global variables.
    """
    dirs = {name: Path(p).resolve() for name, p in skill_dirs.items()}

    # The @tool decorator registers this function as a tool the model can call. `context=True` makes Strands
    # pass in `tool_context`, which gives access to the agent (and so to the list of activated skills).
    # The docstring below is what the model reads to learn how to use the tool, so it is written for the model.
    @tool(name="read_skill_resource", context=True)
    def read_skill_resource(skill_name: str, path: str, tool_context: ToolContext) -> str:
        """Read a resource file (for example under references/) that belongs to an activated skill.

        Args:
            skill_name: Name of the activated skill that owns the file.
            path: Path relative to the skill directory, as listed under "Available resources".
        """
        # Problems are returned as text instead of raised, so the model sees the reason and can recover.
        try:
            return read_resource_text(dirs, skill_name, path, _activated_skills(tool_context.agent))
        except ResourceError as e:
            return f"Error: {e}"

    return read_skill_resource
