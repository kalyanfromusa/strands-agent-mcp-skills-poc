# syntax=docker/dockerfile:1.7
# =====================================================================================================
# chat_relay/Dockerfile: the hosted chat relay Lambda as a container image
#
# What this file does
#   Packages handler.py (Python standard library only, no extra packages) into AWS's official Lambda
#   Python 3.12 base image for arm64. Lambda runs `handler.lambda_handler` for every request that the
#   hosted chat page sends to the function URL: the function checks it and forwards the user's token
#   and message to the AgentCore Runtime (the Runtime can't be called from a browser directly).
#
# Where it fits
#   Built only by GitHub Actions on a native arm64 runner (ci.yml / build.yml build, check and scan it;
#   deploy.yml pushes it to the ECR repository shopassist-chat-relay, tagged with the git SHA).
#   infra/terraform/modules/chat_relay creates the function and its URL from that image.
#
# Step by step
#   1. Start from public.ecr.aws/lambda/python:3.12-arm64 (it already contains the Lambda runtime).
#   2. Copy handler.py into the task root.
#   3. CMD names the function Lambda calls.
#
# Build context: the REPO ROOT, like the other images; chat_relay/Dockerfile.dockerignore
# limits what the root context sends to this one file.
#
# Key terms
#   Task root: the folder Lambda imports the handler from (LAMBDA_TASK_ROOT).
#   Function URL: a public HTTPS address that Lambda gives a function; here the Runtime JWT check decides access.
#   ECR (Elastic Container Registry): AWS's private storage for container images.
#   SHA: the 40-character fingerprint of a git commit, used as the image tag.
#   Non-root: Lambda runs the code as an unprivileged user by itself, so no USER line is needed.
# =====================================================================================================

# Step 1: the official Lambda Python 3.12 image for arm64 (AWS Graviton).
FROM --platform=linux/arm64 public.ecr.aws/lambda/python:3.12-arm64
# Step 1b: install the fixed rpm and sqlite packages (Trivy HIGH findings) that the base image lacks.
# AL2023 locks an image to its release snapshot, so --releasever=latest is needed to see the fixes.
# Remove this line once AWS ships a base image that already contains them.
RUN dnf upgrade -y --releasever=latest rpm rpm-libs sqlite-libs && dnf clean all

# Step 2: the handler, flat in the task root.
COPY chat_relay/handler.py ${LAMBDA_TASK_ROOT}/

# Step 3: module.function that Lambda calls for every event.
CMD ["handler.lambda_handler"]
