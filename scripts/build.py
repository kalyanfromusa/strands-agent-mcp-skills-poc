"""Package the deployable zips into dist/ (design.md section 5).

- dist/agent.zip          agent/* + agent/requirements.txt deps          (entry point app.py)
- dist/support_mcp.zip    mcp_servers/support_mcp/* + common/ + deps     (entry point server.py)
- dist/orders_lambda.zip  handler.py + common/ (boto3 comes from the Lambda runtime)

Dependencies are linux aarch64 / CPython 3.12 wheels installed with uv, so the zips run on
AgentCore Runtime (arm64) even when built on Windows. Zip paths always use forward slashes.

Usage: uv run python scripts/build.py [--only agent|support_mcp|orders_lambda]

What this file does
    Builds the three zip files that get uploaded to AWS: the agent, the support MCP server and the
    orders Lambda. Each zip holds our code plus the Python libraries it needs.

Where it fits
    Run it before every deployment (and after any code change). Both deployment paths, Terraform
    (`infra/terraform`) and the boto3 scripts (`infra/boto3`), upload the zips from dist/. An analyst
    only runs it when deploying; it is not needed for tests or the local demo.

Step by step (for each zip)
    1. Make an empty working folder under build/<name>.
    2. Download the libraries listed in that part's requirements.txt, in the versions built for the
       AWS computers (Linux on ARM processors, Python 3.12), into the folder. The orders Lambda skips
       this: it needs no extra libraries.
    3. Copy our own source files into the folder (and the shared `common/` package where needed),
       skipping caches and secret `.env` files.
    4. Zip the folder into dist/<name>.zip and print its size.

Key terms
    Zip / artifact: the single file that holds everything a service needs to run.
    Dependency / wheel: a ready-made Python library; a "wheel" is the file format it comes in.
    arm64 / aarch64: the processor type AgentCore Runtime runs on (different from most Windows PCs, so
        the libraries must be downloaded for that processor, not for this computer).
    uv: the fast Python package tool this project uses to install libraries.
    Entry point: the file the service runs first (app.py, server.py or handler.py).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import zipfile
from collections.abc import Callable
from pathlib import Path

# The repository root folder (one level above scripts/).
ROOT = Path(__file__).resolve().parents[1]
# Files and folders that must never go into a zip: Python caches, the local virtual environment,
# and `.env` files, which may hold secrets.
EXCLUDED_DIRS = {"__pycache__", ".pytest_cache", ".venv"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
EXCLUDED_NAMES = {".env"}


def _skip(path: Path) -> bool:
    """True if this file should be left out of the zip (a cache file or a secrets file)."""
    return (any(part in EXCLUDED_DIRS for part in path.parts)
            or path.suffix in EXCLUDED_SUFFIXES or path.name in EXCLUDED_NAMES)


def copy_tree(src: Path, dst: Path) -> None:
    """Copy a directory's contents into dst, skipping caches."""
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if path.is_file() and not _skip(rel):
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dst / rel)


def install_deps(requirements: Path, target: Path) -> None:
    """Install linux aarch64 cp312 wheels for `requirements` into `target` with uv.

    The flags tell uv to fetch libraries for the AWS machine (Linux, ARM, Python 3.12), not for this
    PC, and to accept only ready-built wheels ("--only-binary"), because nothing can be compiled for
    Linux on a Windows machine.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv not found on PATH (https://docs.astral.sh/uv/)")
    subprocess.run(
        [uv, "pip", "install", "--quiet", "--target", str(target),
         "--python-platform", "aarch64-manylinux2014", "--python-version", "3.12",
         "--only-binary=:all:", "-r", str(requirements)],
        check=True,
    )


def write_zip(src: Path, zip_path: Path) -> None:
    """Zip src's contents at the zip root with forward-slash paths and readable permissions.

    Why the care: AWS runs the code on Linux. Linux expects "/" in paths (Windows uses "\\") and needs
    each file marked as readable, otherwise the service cannot load it.
    """
    # Step 1: replace any old zip.
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path.unlink(missing_ok=True)
    # Step 2: add every file (except skipped ones) with a Linux-style path and permissions.
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(src.rglob("*")):
            rel = path.relative_to(src)
            if not path.is_file() or _skip(rel):
                continue
            info = zipfile.ZipInfo.from_file(path, rel.as_posix())
            info.external_attr = 0o100755 << 16  # rwxr-xr-x regular file, regardless of Windows ACLs
            info.compress_type = zipfile.ZIP_DEFLATED
            with path.open("rb") as fh, zf.open(info, "w") as out:
                shutil.copyfileobj(fh, out)


def _stage(root: Path, name: str) -> Path:
    """Create a fresh, empty working folder build/<name> (any older one is deleted first)."""
    staging = root / "build" / name
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    return staging


def build_agent(root: Path = ROOT) -> Path:
    """dist/agent.zip: agent sources + their dependencies at the zip root.

    This is the generic agent that AgentCore Runtime runs. The skills are not inside this zip; they
    are uploaded separately (to S3) by the deployment.
    """
    src = root / "agent"
    requirements = src / "requirements.txt"
    if not requirements.is_file():
        raise SystemExit(f"missing {requirements}")
    staging = _stage(root, "agent")
    # Libraries first, then our code, so our files are what ends up in the zip if names ever overlap.
    install_deps(requirements, staging)
    copy_tree(src, staging)
    zip_path = root / "dist" / "agent.zip"
    write_zip(staging, zip_path)
    return zip_path


def build_support_mcp(root: Path = ROOT) -> Path:
    """dist/support_mcp.zip: server.py, faq.json, common/ and dependencies.

    This is the support MCP server that a second AgentCore Runtime hosts.
    """
    src = root / "mcp_servers" / "support_mcp"
    staging = _stage(root, "support_mcp")
    install_deps(src / "requirements.txt", staging)
    copy_tree(src, staging)
    # The shared data and rules code goes in as the package "common", next to server.py.
    copy_tree(root / "mcp_servers" / "common", staging / "common")
    zip_path = root / "dist" / "support_mcp.zip"
    write_zip(staging, zip_path)
    return zip_path


def build_orders_lambda(root: Path = ROOT) -> Path:
    """dist/orders_lambda.zip: handler.py + common/ (no third-party deps).

    No libraries to install: the only one it needs, boto3 (for DynamoDB), is already provided by
    AWS Lambda's Python environment. That keeps this zip tiny.
    """
    staging = _stage(root, "orders_lambda")
    shutil.copy2(root / "mcp_servers" / "orders_lambda" / "handler.py", staging / "handler.py")
    copy_tree(root / "mcp_servers" / "common", staging / "common")
    zip_path = root / "dist" / "orders_lambda.zip"
    write_zip(staging, zip_path)
    return zip_path


# The names accepted by --only, and the function that builds each zip.
BUILDERS: dict[str, Callable[[Path], Path]] = {
    "agent": build_agent,
    "support_mcp": build_support_mcp,
    "orders_lambda": build_orders_lambda,
}


def main() -> None:
    """Build all three zips (or just the one named with --only) and print each zip's size in MB."""
    parser = argparse.ArgumentParser(description="Build deployment zips into dist/")
    parser.add_argument("--only", choices=sorted(BUILDERS), help="build a single artifact")
    args = parser.parse_args()
    for name in [args.only] if args.only else list(BUILDERS):
        zip_path = BUILDERS[name](ROOT)
        print(f"{zip_path.relative_to(ROOT).as_posix():<24} {zip_path.stat().st_size / 1_048_576:8.2f} MB")


if __name__ == "__main__":
    main()
