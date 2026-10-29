# syntax=docker/dockerfile:1.7
# =====================================================================================================
# mcp_servers/support_mcp/Dockerfile: the `support` MCP server as a container image (container mode)
#
# What this file does
#   Packages server.py, faq.json, the shared `common` package and the libraries in requirements.txt
#   into a linux/arm64 image that AgentCore Runtime can run instead of dist/support_mcp.zip. The server
#   speaks stateless streamable HTTP MCP on 0.0.0.0:8000 at /mcp, exactly as in zip mode.
#
# Where it fits
#   Built by .github/workflows/deploy.yml, pushed to the ECR repository shopassist-support-mcp
#   (infra/terraform/ci), rolled out with scripts/ci/update_runtime_image.py, after which the Gateway's
#   `support` target is re-synchronized so it re-reads the tool list.
#
# Step by step
#   1. Start from the official Python 3.12 slim image for arm64.
#   2. Copy in uv and install the requirements (one cached layer).
#   3. Lay the code out like scripts/build.py lays out the zip:
#        /app/server.py, /app/faq.json, /app/common/{__init__,store,rules}.py
#      so `from common.store import get_store` works with /app as the working directory.
#   4. Switch to an unprivileged user and start `python server.py`.
#
# Build context: the REPO ROOT (needed because mcp_servers/common is outside this folder), with -f:
#   docker buildx build --platform linux/arm64 -f mcp_servers/support_mcp/Dockerfile -t shopassist-support-mcp:dev .
#   mcp_servers/support_mcp/Dockerfile.dockerignore limits what the root context sends.
#
# Key terms
#   MCP (Model Context Protocol): the standard way an agent lists and calls tools.
#   Stateless streamable HTTP: each MCP request stands alone, which is what AgentCore Runtime requires.
#   common: the shared data/rules package also bundled into the orders Lambda zip.
# =====================================================================================================

FROM --platform=linux/arm64 python:3.12-slim

# COMPILE_BYTECODE=1 (default, used in CI on native arm64): pre-compile .pyc for a faster cold start.
# Pass --build-arg COMPILE_BYTECODE=0 for local builds under QEMU emulation, where compiling the
# dependencies takes ~10 minutes instead of seconds.
ARG COMPILE_BYTECODE=1

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=${COMPILE_BYTECODE} \
    UV_NO_CACHE=1

WORKDIR /app

# Step 2: dependencies first (cache-friendly).
COPY mcp_servers/support_mcp/requirements.txt /tmp/requirements.txt
RUN uv pip install --system -r /tmp/requirements.txt && rm /tmp/requirements.txt

# Step 3: same layout as dist/support_mcp.zip (scripts/build.py: build_support_mcp).
COPY mcp_servers/support_mcp/server.py mcp_servers/support_mcp/faq.json /app/
COPY mcp_servers/common/ /app/common/

# Step 4: unprivileged user; the server writes nothing to disk (tickets go to DynamoDB).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin mcp
USER 10001

EXPOSE 8000
CMD ["python", "server.py"]
