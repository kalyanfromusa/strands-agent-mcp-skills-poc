# syntax=docker/dockerfile:1.7
# =====================================================================================================
# agent/Dockerfile: the generic ShopAssist agent as a container image
#
# What this file does
#   Packages agent/*.py and the exact library versions in agent/requirements.lock.txt (pinned from
#   uv.lock, with hashes) into a linux/arm64 image for AgentCore Runtime. Entry point `python app.py`:
#   the SDK's web server answers POST /invocations and GET /ping on 0.0.0.0:8080.
#
# Where it fits
#   Built only by GitHub Actions on a native arm64 runner (ci.yml / build.yml build and check it;
#   deploy.yml also pushes it to the ECR repository shopassist-agent from infra/terraform/platform,
#   tagged with the git commit SHA). Terraform then points the agent runtime at <repo>:<sha>.
#
# Step by step
#   1. Start from the official Python 3.12 slim image for arm64 (AgentCore Runtime runs on Graviton).
#   2. Copy in the uv binary and install the pinned, hash-checked requirements (one cached layer).
#   3. Copy the agent's own modules (flat layout: app.py next to config.py etc.).
#   4. Switch to an unprivileged user and start `python app.py`.
#
# Build context: the REPO ROOT (so both images share one convention), with -f:
#   docker buildx build --platform linux/arm64 -f agent/Dockerfile -t shopassist-agent:dev .
#   agent/Dockerfile.dockerignore (next to this file) limits what the root context sends.
#
# Key terms
#   arm64 / Graviton: the CPU type AgentCore Runtime uses; images must be built for linux/arm64.
#   uv: a fast Python package installer.
#   --require-hashes: refuse any package whose file fingerprint differs from the lock file.
#   /ping: the health check AgentCore calls; /invocations: where each user request arrives.
# =====================================================================================================

FROM --platform=linux/arm64 python:3.12-slim

# Security updates for the base image's OS packages (the slim image can lag behind Debian's fixes;
# Trivy in CI fails the build on fixable HIGH/CRITICAL findings). One layer, apt lists removed after.
RUN apt-get update \
    && apt-get -y upgrade --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# COMPILE_BYTECODE=1 (default, used in CI on native arm64): pre-compile .pyc for a faster cold start.
# Pass --build-arg COMPILE_BYTECODE=0 for a build under QEMU emulation (x86 runner), where compiling the
# dependencies takes ~10 minutes instead of seconds.
ARG COMPILE_BYTECODE=1

# Pinned uv release, copied from its official image (no pip bootstrap, no curl needed).
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

# PYTHONUNBUFFERED: log lines reach CloudWatch immediately. UV_*: install into the image's Python,
# compile .pyc at build time (faster cold start), keep no download cache in the layer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=${COMPILE_BYTECODE} \
    UV_NO_CACHE=1 \
    PORT=8080 \
    AGENT_HOST=0.0.0.0

WORKDIR /app

# Step 2: dependencies first, so a code-only change reuses this (large) layer from the build cache.
COPY agent/requirements.lock.txt /tmp/requirements.lock.txt
RUN uv pip install --system --require-hashes -r /tmp/requirements.lock.txt && rm /tmp/requirements.lock.txt

# Step 3: the agent modules, flat at /app (app.py adds its own folder to sys.path).
COPY agent/*.py /app/

# Step 4: unprivileged user. Skills are synced to /tmp/skills at cold start (SKILLS_S3_URI), and /tmp
# is writable for everyone, so nothing under /app needs to be writable.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin agent
USER 10001

EXPOSE 8080
CMD ["python", "app.py"]
