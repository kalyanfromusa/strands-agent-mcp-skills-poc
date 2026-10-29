"""Where skills come from: an S3 prefix synced to local disk, or a local directory.

What this file does
    Finds the folder that holds the skills (one sub-folder per skill, each with a `SKILL.md`). In the cloud the
    skills are kept in an S3 bucket, so this file first downloads them to a local scratch folder. Locally it
    simply points at the repo's `skills/` folder.

Where it fits
    `app.py` (`AgentRuntime.skills()`) calls `resolve_skills_dir()` once, the first time the agent is built. The
    folder it returns is handed to the Strands `Skill.from_directory()` loader. Keeping skills in S3 means the
    business procedures can be updated by uploading new files, without redeploying the agent code.

Step by step
    1. `resolve_skills_dir()` checks the settings: is `SKILLS_S3_URI` set?
    2. If yes, `sync_s3_skills()` empties the local cache folder, lists every file under the S3 location,
       skips any file name that could escape the folder, and downloads the rest.
    3. If no, it uses the local `SKILLS_DIR` folder as it is.
    4. It returns the full path of the folder to use.

Key terms
    S3: Amazon Simple Storage Service, AWS file storage. Files are "objects" and their names are "keys".
    S3 URI: an address like `s3://my-bucket/skills/` (bucket name, then a folder-like "prefix").
    Prefix: the start of the object names that make up one "folder" in S3.
    boto3: the official Python library for calling AWS services.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger(__name__)


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """`s3://bucket/some/prefix/` -> (`bucket`, `some/prefix/`).

    Splits an S3 address into the bucket name and the folder prefix. A trailing "/" is added to the prefix so
    that `skills` doesn't also match a neighbour like `skills-old/`.
    """
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3:// URI: {uri!r}")
    rest = uri[5:]
    bucket, _, prefix = rest.partition("/")
    if not bucket:
        raise ValueError(f"missing bucket in {uri!r}")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return bucket, prefix


def _safe_relative(key: str, prefix: str) -> PurePosixPath | None:
    """Turn an S3 object name into a path inside the skills folder, or None if it is unsafe.

    Why: S3 object names are free text. A name containing `..` or starting with `/` could make the download
    write a file outside the skills folder. Such names are rejected (a security safeguard).
    """
    rel = PurePosixPath(key[len(prefix) :])
    if not rel.parts or rel.is_absolute() or any(p in ("..", "") for p in rel.parts):
        return None
    return rel


def sync_s3_skills(uri: str, dest: str | Path, s3_client: Any | None = None) -> Path:
    """Download every object under the S3 prefix into `dest` (replacing its contents).

    Tests pass a fake `s3_client`; in real use a boto3 S3 client is created here.
    """
    # Step 1: work out which bucket and folder to read, and connect to S3.
    bucket, prefix = parse_s3_uri(uri)
    if s3_client is None:
        import boto3

        s3_client = boto3.client("s3")
    # Step 2: start from an empty folder, so skills deleted from S3 also disappear locally.
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    # Step 3: list the files. The S3 ListObjectsV2 API call returns at most 1,000 names per response,
    # so the "paginator" keeps asking for the next page until all names are returned.
    for page in s3_client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            # Names ending in "/" are empty "folder marker" objects, not real files.
            if key.endswith("/"):
                continue
            rel = _safe_relative(key, prefix)
            if rel is None:
                logger.warning("skipping unsafe S3 key %s", key)
                continue
            # Step 4: recreate the sub-folder (for example `returns-and-refunds/references/`) and download the file
            # with the S3 download_file call.
            target = dest.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            s3_client.download_file(bucket, key, str(target))
            count += 1
    logger.info("synced %d skill files from %s to %s", count, uri, dest)
    return dest


def resolve_skills_dir(settings: Any, s3_client: Any | None = None) -> Path:
    """The local directory holding skill folders, syncing from S3 first when configured.

    S3 wins when `SKILLS_S3_URI` is set (the deployed setup). Otherwise the local `SKILLS_DIR` is used.
    """
    if settings.skills_s3_uri:
        return sync_s3_skills(settings.skills_s3_uri, settings.skills_cache_dir, s3_client=s3_client).resolve()
    return Path(settings.skills_dir).resolve()
