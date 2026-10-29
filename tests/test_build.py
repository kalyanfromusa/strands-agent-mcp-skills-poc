"""scripts/build.py: zip layout, forward-slash paths, cache exclusion (dependency install stubbed)."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from scripts import build

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    """A throwaway project root: real mcp_servers/ copy plus a stub agent/."""
    shutil.copytree(ROOT / "mcp_servers", tmp_path / "mcp_servers")
    (tmp_path / "mcp_servers" / "common" / "__pycache__").mkdir(exist_ok=True)
    (tmp_path / "mcp_servers" / "common" / "__pycache__" / "store.cpython-312.pyc").write_bytes(b"x")
    agent = tmp_path / "agent"
    (agent / "pkg").mkdir(parents=True)
    (agent / "app.py").write_text("print('hi')\n")
    (agent / "pkg" / "mod.py").write_text("")
    (agent / "requirements.txt").write_text("fakedep\n")

    def fake_install(requirements: Path, target: Path) -> None:
        assert requirements.is_file()
        (target / "fakedep").mkdir(parents=True, exist_ok=True)
        (target / "fakedep" / "__init__.py").write_text("")

    monkeypatch.setattr(build, "install_deps", fake_install)
    return tmp_path


def names(zip_path: Path) -> set[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return set(zf.namelist())


def assert_clean(entries: set[str]) -> None:
    assert all("\\" not in n for n in entries)
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in entries)


def test_orders_lambda_zip(project):
    entries = names(build.build_orders_lambda(project))
    assert entries == {"handler.py", "common/__init__.py", "common/rules.py", "common/store.py"}


def test_support_mcp_zip(project):
    entries = names(build.build_support_mcp(project))
    assert {"server.py", "faq.json", "requirements.txt", "common/store.py", "common/rules.py",
            "fakedep/__init__.py"} <= entries
    assert_clean(entries)


def test_agent_zip(project):
    zip_path = build.build_agent(project)
    entries = names(zip_path)
    assert {"app.py", "pkg/mod.py", "requirements.txt", "fakedep/__init__.py"} <= entries
    assert_clean(entries)
    with zipfile.ZipFile(zip_path) as zf:
        assert (zf.getinfo("app.py").external_attr >> 16) & 0o444 == 0o444


def test_agent_requires_requirements(project):
    (project / "agent" / "requirements.txt").unlink()
    with pytest.raises(SystemExit):
        build.build_agent(project)
