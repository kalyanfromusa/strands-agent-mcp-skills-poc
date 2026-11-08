# syntax=docker/dockerfile:1.7
# =====================================================================================================
# identity/role_mapper/Dockerfile: the Cognito role-mapper Lambda as a container image
#
# What this file does
#   Packages handler.py (standard library only) into AWS's Lambda Python 3.12 base image for arm64.
#
# Where it fits
#   Built only by GitHub Actions; deploy.yml pushes it to ECR shopassist-role-mapper:<git sha>.
#   infra/terraform/modules/role_mapper creates the function when enable_entra = true, and Cognito calls
#   it before issuing tokens (pre-token-generation trigger).
#
# Step by step
#   1. Start from public.ecr.aws/lambda/python:3.12-arm64.
#   2. Copy handler.py into the task root.
#   3. CMD names the function Lambda calls.
#
# Key terms
#   Task root: the folder Lambda imports the handler from (LAMBDA_TASK_ROOT).
# =====================================================================================================
FROM --platform=linux/arm64 public.ecr.aws/lambda/python:3.12-arm64
COPY identity/role_mapper/handler.py ${LAMBDA_TASK_ROOT}/
CMD ["handler.lambda_handler"]
