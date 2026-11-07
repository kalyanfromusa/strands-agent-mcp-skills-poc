# syntax=docker/dockerfile:1.7
# =====================================================================================================
# mcp_servers/orders_lambda/Dockerfile: the `orders` tools Lambda function as a container image
#
# What this file does
#   Packages handler.py and the shared `common` package (data store + return rules) into AWS's official
#   Lambda base image for Python 3.12 on arm64. Lambda runs `handler.lambda_handler` for each tool call.
#
# Where it fits
#   Built only by GitHub Actions (arm64 runner); deploy.yml pushes it to the ECR repository
#   shopassist-orders-lambda (infra/terraform/platform), tagged with the git SHA. Terraform's
#   modules/lambda_tools creates the function with package_type = "Image" and that image URI. The
#   AgentCore Gateway's `orders` target invokes the function for get_order, create_return, and so on.
#
# Step by step
#   1. Start from public.ecr.aws/lambda/python:3.12-arm64. It already contains the Lambda runtime
#      interface (the part that receives events) and boto3, the only library the code needs.
#   2. Copy handler.py and common/ into the task root, /var/task (LAMBDA_TASK_ROOT).
#   3. Tell Lambda which function to call: CMD ["handler.lambda_handler"].
#
# Build context: the REPO ROOT (mcp_servers/common is outside this folder), with -f:
#   docker buildx build --platform linux/arm64 --provenance=false -f mcp_servers/orders_lambda/Dockerfile .
#   mcp_servers/orders_lambda/Dockerfile.dockerignore limits what the root context sends.
#
# Key terms
#   Lambda base image: an AWS-maintained image that behaves exactly like the managed Python runtime.
#   Task root: the folder Lambda imports the handler from.
#   Non-root: Lambda runs the code as an unprivileged user by itself, so no USER line is needed.
# =====================================================================================================

# Step 1: the official Lambda Python 3.12 image for arm64 (AWS Graviton).
FROM --platform=linux/arm64 public.ecr.aws/lambda/python:3.12-arm64

# Step 2: the handler and the shared package, flat in the task root (import `common.store` works).
COPY mcp_servers/orders_lambda/handler.py ${LAMBDA_TASK_ROOT}/
COPY mcp_servers/common/ ${LAMBDA_TASK_ROOT}/common/

# Step 3: module.function that Lambda calls for every event.
CMD ["handler.lambda_handler"]
