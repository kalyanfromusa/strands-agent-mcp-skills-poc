"""S3 artifacts bucket: the code zips and the skills.

What this file does
    Deploy step 1. Creates the file storage bucket and uploads the three code packages and the skill
    instruction files into it.

Where it fits
    Everything after it reads from this bucket: the Lambda (step 7) and both runtimes (steps 8 and 11)
    load their code from it, and the deployed agent downloads its skills from it when it starts.

AgentCore Runtime "direct code deployment" pulls a zip from S3 instead of a container image from ECR,
and Lambda can be created from an S3 object too. The bucket is **versioned**: every upload of a changed
zip gets a new VersionId, and the runtimes/Lambda are pinned to that version, so a rebuilt zip shows up
as an in-place update (see `mcp_runtime.py` / `agent_runtime.py`).

Skills are plain files under `skills/`. The agent syncs them from `SKILLS_S3_URI` at cold start, so a
skill edit only needs a re-deploy of this step and a new session, not a new agent zip.

Layout: s3://<bucket>/artifacts/<name>.zip and s3://<bucket>/skills/<skill>/...

Step by step
    1. Create the bucket if it does not exist (and check it really belongs to this account).
    2. Turn on versioning, block all public access, and tag it.
    3. Upload each code zip, but only if it changed; remember each zip's version ID.
    4. Mirror the local `skills/` folder: upload changed files, delete files that were removed locally.

Key terms
    S3 (Simple Storage Service): AWS file storage. A "bucket" is a top-level folder; files are "objects".
    Object key: the full path of a file inside the bucket, e.g. artifacts/agent.zip.
    Versioning: S3 keeps every old copy of a file; each copy has its own version ID.
    ETag / MD5: a fingerprint of a file's contents, used here to skip uploading unchanged files.
    Cold start: the first start of a new agent session.
"""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

from common import ARTIFACTS, SKILLS_DIR, Context, OK, ignore_not_found, info, is_not_found, tag_list

# Folder names inside the bucket.
ARTIFACT_PREFIX = "artifacts/"
SKILLS_PREFIX = "skills/"


# ------------------------------------------------------------------ request builders (pure)


def build_create_bucket_request(bucket: str, region: str) -> dict:
    """CreateBucket request (the region is given explicitly everywhere except us-east-1)."""
    req: dict = {"Bucket": bucket}
    if region != "us-east-1":  # us-east-1 is the one region that rejects an explicit LocationConstraint
        req["CreateBucketConfiguration"] = {"LocationConstraint": region}
    return req


def build_versioning_request(bucket: str) -> dict:
    """PutBucketVersioning request: keep every version of every file (needed to pin code versions)."""
    return {"Bucket": bucket, "VersioningConfiguration": {"Status": "Enabled"}}


def build_public_access_block_request(bucket: str) -> dict:
    """PutPublicAccessBlock request: make sure nothing in the bucket can ever be made public."""
    return {
        "Bucket": bucket,
        "PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        },
    }


def build_tagging_request(bucket: str, tags: dict[str, str]) -> dict:
    """PutBucketTagging request: label the bucket with the project tag."""
    return {"Bucket": bucket, "Tagging": {"TagSet": tag_list(tags)}}


# ------------------------------------------------------------------ helpers


def md5_hex(path: Path) -> str:
    """MD5 fingerprint of a local file, read in 1 MB pieces so big files don't fill memory."""
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _content_type(path: Path) -> str:
    """File type label stored with the object (e.g. text/markdown for SKILL.md)."""
    if path.suffix == ".md":
        return "text/markdown"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def upload_if_changed(s3, bucket: str, key: str, path: Path) -> tuple[str | None, bool]:
    """Upload `path` unless the current object already has the same MD5. Returns (VersionId, changed).

    A single-part put_object's ETag is the MD5 of the body, so it doubles as a change detector.
    Skipping unchanged files keeps the same version ID, which means the runtimes and Lambda see no
    change and are not updated needlessly.
    """
    local_md5 = md5_hex(path)
    # Step 1: AWS API HeadObject reads the stored file's details (not its contents).
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
        if head["ETag"].strip('"') == local_md5:
            return head.get("VersionId"), False
    except Exception as err:  # noqa: BLE001 - 404 means "not uploaded yet"
        if not is_not_found(err) and "404" not in str(err):
            raise
    # Step 2: new or changed -> AWS API PutObject uploads it; S3 returns the new version ID.
    with path.open("rb") as body:
        resp = s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=_content_type(path))
    return resp.get("VersionId"), True


# ------------------------------------------------------------------ deploy / destroy


def deploy(ctx: Context) -> None:
    """Create and configure the bucket, then upload the code zips and the skills."""
    s3 = ctx.client("s3")
    n = ctx.names
    bucket = n.bucket

    # Step 1: AWS API HeadBucket checks the bucket exists AND is owned by our account
    # (ExpectedBucketOwner). 404 means create it; any other error (e.g. someone else owns the name) stops.
    try:
        s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=ctx.account_id)
        info(f"bucket {bucket} exists")
    except Exception as err:  # noqa: BLE001
        if "404" not in str(err) and not is_not_found(err):
            raise RuntimeError(f"Bucket {bucket} exists but is not accessible from this account: {err}") from err
        s3.create_bucket(**build_create_bucket_request(bucket, ctx.region))
        s3.get_waiter("bucket_exists").wait(Bucket=bucket)
        info(f"{OK} created bucket {bucket}")

    # Step 2: versioning, public access block, tags.
    # These three calls are idempotent "put" operations, so they simply run every time.
    s3.put_bucket_versioning(**build_versioning_request(bucket))
    s3.put_public_access_block(**build_public_access_block_request(bucket))
    s3.put_bucket_tagging(**build_tagging_request(bucket, n.tags))
    ctx.state.put("s3", bucket=bucket)

    # Step 3: code zips. The saved version IDs are what the Lambda and runtimes get pinned to.
    # Code zips (built by scripts/build.py; deploy.py has already checked they exist).
    artifacts = {}
    for name, path in ARTIFACTS.items():
        key = ARTIFACT_PREFIX + path.name
        version_id, changed = upload_if_changed(s3, bucket, key, path)
        artifacts[name] = {"key": key, "version_id": version_id}
        info(f"{'uploaded' if changed else 'unchanged'} s3://{bucket}/{key} (version {version_id})")
    ctx.state.put("s3", artifacts=artifacts)

    # Step 4: skills.
    # Skills: mirror the local folder, including deletions.
    local_keys = set()
    uploaded = 0
    for path in sorted(p for p in SKILLS_DIR.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        key = SKILLS_PREFIX + path.relative_to(SKILLS_DIR).as_posix()
        local_keys.add(key)
        _, changed = upload_if_changed(s3, bucket, key, path)
        uploaded += changed
    # Files in the bucket's skills/ folder that no longer exist locally (AWS API ListObjectsV2).
    stale = [
        obj["Key"]
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=SKILLS_PREFIX)
        for obj in page.get("Contents", [])
        if obj["Key"] not in local_keys
    ]
    # AWS API DeleteObjects removes up to 1000 files per call, hence the batches.
    for i in range(0, len(stale), 1000):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in stale[i:i + 1000]]})
    info(f"skills: {len(local_keys)} files, {uploaded} uploaded, {len(stale)} removed")
    # The agent reads this address from its SKILLS_S3_URI setting.
    ctx.state.put("s3", skills_s3_uri=f"s3://{bucket}/{SKILLS_PREFIX}")


def destroy(ctx: Context) -> None:
    """A versioned bucket can only be deleted once every object version and delete marker is gone."""
    s3 = ctx.client("s3")
    bucket = ctx.state.section("s3").get("bucket", ctx.names.bucket)
    # Step 1: does the bucket still exist?
    try:
        s3.head_bucket(Bucket=bucket)
    except Exception:  # noqa: BLE001
        info(f"bucket {bucket} already gone")
        ctx.state.drop("s3")
        return
    # Step 2: list every version of every file, plus "delete markers" (S3's record that a file was
    # deleted), and delete them all, in batches of up to 1000 (AWS API ListObjectVersions + DeleteObjects).
    deleted = 0
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
        batch = [{"Key": v["Key"], "VersionId": v["VersionId"]}
                 for v in page.get("Versions", []) + page.get("DeleteMarkers", [])]
        for i in range(0, len(batch), 1000):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": batch[i:i + 1000], "Quiet": True})
        deleted += len(batch)
    # Step 3: the now-empty bucket can be deleted (AWS API DeleteBucket).
    ignore_not_found(s3.delete_bucket, Bucket=bucket)
    info(f"{OK} deleted bucket {bucket} ({deleted} object versions)")
    ctx.state.drop("s3")
