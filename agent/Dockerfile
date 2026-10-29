# syntax=docker/dockerfile:1.7
# =====================================================================================================
# agent/Dockerfile: the generic ShopAssist agent as a container image (container mode only)
#
# What this file does
#   Packages agent/*.py and the libraries in agent/requirements.txt into a linux/arm64 image that
#   AgentCore Runtime can run instead of dist/agent.zip. Same code, same entry point (`python app.py`),
#   same port: the SDK's web server answers POST /invocations and GET /ping on 0.0.0.0:8080.
#
# Where it fits
#   Built by .github/workflows/deploy.yml (or by hand, see below), pushed to the ECR repository
#   shopassist-agent created by infra/terraform/ci, then rolled out with
#   scripts/ci/update_runtime_image.py. Zip mode (scripts/build.py) stays the default; this file is
#   only used when the main stack runs with artifact_type = "container" (docs/10-ci-cd.md).
#
# Step by step
#   1. Start from the official Python 3.12 slim image for arm64 (AgentCore Runtime runs on Graviton).
#   2. Copy in the uv binary and install the requirements into the system Python (one cached layer).
#   3. Copy the agent's own modules (flat layout, as in the zip: app.py next to config.py etc.).
#   4. Switch to an unprivileged user and start `python app.py`.
#
# Build context: the REPO ROOT (so both images share one convention), with -f:
#   docker buildx build --platform linux/arm64 -f agent/Dockerfile -t shopassist-agent:dev .
#   agent/Dockerfile.dockerignore (next to this file) limits what the root context sends.
#
# Key terms
#   arm64 / Graviton: the CPU type AgentCore Runtime uses; images must be built for linux/arm64.
#   uv: a fast Python package installer (the same tool scripts/build.py uses for the zips).
#   /ping: the health check AgentCore calls; /invocations: where each user request arrives.
# =====================================================================================================

FROM --platform=linux/arm64 python:3.12-slim

# COMPILE_BYTECODE=1 (default, used in CI on native arm64): pre-compile .pyc for a faster cold start.
# Pass --build-arg COMPILE_BYTECODE=0 for local builds under QEMU emulation, where compiling the
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
COPY agent/requirements.txt /tmp/requirements.txt
RUN uv pip install --system -r /tmp/requirements.txt && rm /tmp/requirements.txt

# Step 3: the agent modules, at /app like the zip root (app.py adds its own folder to sys.path).
COPY agent/*.py /app/

# Step 4: unprivileged user. Skills are synced to /tmp/skills at cold start (SKILLS_S3_URI), and /tmp
# is writable for everyone, so nothing under /app needs to be writable.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin agent
USER 10001

EXPOSE 8080
CMD ["python", "app.py"]
