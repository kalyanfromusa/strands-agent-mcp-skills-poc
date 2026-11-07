"""The image lock files (requirements.lock.txt) must match uv.lock exactly, and carry hashes.

What this file does
    Reads uv.lock and each image's requirements.lock.txt, then checks, offline:
      - every pinned package has exactly the version uv.lock resolved;
      - every pin has at least one sha256 hash (so `--require-hashes` installs work);
      - every direct dependency in requirements.in appears in its lock file.

Where it fits
    scripts/pin_requirements.py writes the lock files. If someone upgrades uv.lock or edits a
    requirements.in without re-running it, this test fails in CI before an image with drifted versions
    can be built.

Step by step
    1. Parse uv.lock (TOML) into {package name: version}.
    2. Parse a lock file's `name==version` lines and their `--hash=sha256:` lines.
    3. Compare.

Key terms
    Normalised name: package names compared in lowercase, with "_" and "." treated as "-" (PEP 503).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LOCKS = {
    "agent": ROOT / "agent",
    "support_mcp": ROOT / "mcp_servers" / "support_mcp",
}


def norm(name: str) -> str:
    """PEP 503 normalised package name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def uv_lock_versions() -> dict[str, str]:
    """Step 1: {package: version} from uv.lock."""
    data = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {norm(p["name"]): p["version"] for p in data["package"] if "version" in p}


def parse_lock(path: Path) -> dict[str, tuple[str, int]]:
    """Step 2: {package: (version, number of hashes)} from a requirements.lock.txt."""
    pins: dict[str, tuple[str, int]] = {}
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s\\;]+)", line)
        if m:
            current = norm(m.group(1))
            pins[current] = (m.group(2), 0)
        if current and "--hash=sha256:" in line:
            version, count = pins[current]
            pins[current] = (version, count + line.count("--hash=sha256:"))
    return pins


@pytest.mark.parametrize("image", sorted(LOCKS))
def test_lock_matches_uv_lock(image: str) -> None:
    """Step 3: same versions as uv.lock, and every pin hashed."""
    lock = LOCKS[image] / "requirements.lock.txt"
    assert lock.exists(), f"{lock} missing: run `uv run python scripts/pin_requirements.py`"
    expected = uv_lock_versions()
    pins = parse_lock(lock)
    assert pins, f"{lock} has no pins"
    for name, (version, hashes) in pins.items():
        assert name in expected, f"{image}: {name} is pinned but not in uv.lock"
        assert version == expected[name], f"{image}: {name}=={version}, uv.lock has {expected[name]}"
        assert hashes > 0, f"{image}: {name} has no sha256 hash"


@pytest.mark.parametrize("image", sorted(LOCKS))
def test_direct_dependencies_are_locked(image: str) -> None:
    """Every package named in requirements.in ends up in the lock file."""
    pins = parse_lock(LOCKS[image] / "requirements.lock.txt")
    for line in (LOCKS[image] / "requirements.in").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name = norm(re.split(r"[<>=!~\[ ;]", line, maxsplit=1)[0])
        assert name in pins, f"{image}: direct dependency {name} is not in requirements.lock.txt"
