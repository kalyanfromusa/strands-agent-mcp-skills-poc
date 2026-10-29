"""Generic agent: the `read_skill_resource` tool and its path-traversal protection."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

import tools  # noqa: E402

SKILL = "returns-and-refunds"


class _State:
    def __init__(self, activated: list[str] | None) -> None:
        self._activated = activated

    def get(self, key: str):
        assert key == tools.SKILLS_STATE_KEY
        return None if self._activated is None else {"activated_skills": self._activated}


def _ctx(activated: list[str] | None = (SKILL,)):  # type: ignore[assignment]
    return types.SimpleNamespace(agent=types.SimpleNamespace(state=_State(list(activated) if activated else activated)))


@pytest.fixture
def skill_tree(tmp_path) -> dict[str, Path]:
    """Two skills side by side, plus a secret file next to them."""
    a = tmp_path / "skills" / SKILL
    (a / "references").mkdir(parents=True)
    (a / "SKILL.md").write_text("---\nname: x\n---\nbody", encoding="utf-8")
    (a / "references" / "policy.md").write_text("the policy", encoding="utf-8")
    b = tmp_path / "skills" / "other-skill"
    b.mkdir()
    (b / "SKILL.md").write_text("other", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    return {SKILL: a, "other-skill": b}


@pytest.fixture
def read(skill_tree):
    tool = tools.make_read_skill_resource(skill_tree)

    def call(path: str, skill: str = SKILL, activated=(SKILL,)) -> str:
        return tool(skill_name=skill, path=path, tool_context=_ctx(activated))

    return call


def test_tool_spec():
    tool = tools.make_read_skill_resource({})
    spec = tool.tool_spec
    assert spec["name"] == "read_skill_resource"
    assert spec["inputSchema"]["json"]["required"] == ["skill_name", "path"]
    assert "tool_context" not in spec["inputSchema"]["json"]["properties"]


def test_reads_file_of_activated_skill(read):
    assert read("references/policy.md") == "the policy"
    assert read("references\\policy.md") == "the policy"
    assert read("./references/../SKILL.md").endswith("body")


def test_reads_real_skill_reference():
    tool = tools.make_read_skill_resource({SKILL: ROOT / "skills" / SKILL})
    text = tool(skill_name=SKILL, path="references/return-policy.md", tool_context=_ctx())
    assert not text.startswith("Error") and len(text) > 50


@pytest.mark.parametrize(
    "path",
    [
        "../secret.txt",
        "../../secret.txt",
        "references/../../secret.txt",
        "..\\secret.txt",
        "../other-skill/SKILL.md",
        "..",
    ],
)
def test_rejects_traversal(read, path):
    out = read(path)
    assert out.startswith("Error:") and ("escapes" in out or "no such file" in out)
    assert "top secret" not in out and "other" not in out.replace("Error:", "")


@pytest.mark.parametrize("path_fn", [lambda t: str(t / "secret.txt"), lambda t: "/etc/passwd", lambda t: "C:/Windows/win.ini",
                                     lambda t: "\\\\server\\share\\x", lambda t: "C:secret.txt"])
def test_rejects_absolute_paths(read, skill_tree, path_fn):
    out = read(path_fn(skill_tree[SKILL].parents[1]))
    assert out == "Error: path must be relative to the skill directory"


def test_rejects_unknown_skill(read):
    out = read("SKILL.md", skill="nope", activated=None)
    assert out.startswith("Error: unknown skill 'nope'") and "other-skill" in out


def test_rejects_inactive_skill(read):
    out = read("SKILL.md", skill="other-skill", activated=[SKILL])
    assert out.startswith("Error: skill 'other-skill' is not active")


def test_rejects_missing_empty_and_nul(read):
    assert read("references/missing.md").startswith("Error: no such file")
    assert read("references").startswith("Error: no such file")  # a directory is not a file
    assert read("  ").startswith("Error: path is required")
    assert read("a\x00b") == "Error: invalid path"


def test_rejects_large_files(read, skill_tree):
    (skill_tree[SKILL] / "big.txt").write_bytes(b"x" * (tools.MAX_RESOURCE_BYTES + 1))
    assert "larger than" in read("big.txt")


def test_resolve_skill_resource_direct(skill_tree):
    base = skill_tree[SKILL]
    assert tools.resolve_skill_resource(base, "references/policy.md") == (base / "references" / "policy.md").resolve()
    with pytest.raises(tools.ResourceError):
        tools.resolve_skill_resource(base, "../secret.txt")
