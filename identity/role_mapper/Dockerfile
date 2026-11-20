# syntax=docker/dockerfile:1.7
# =====================================================================================================
# identity/role_mapper/Dockerfile: the Cognito role-mapper Lambda as a container image
#
# What this file does
#   Packages handler.py (Python standard library only, no extra packages) into AWS's official Lambda
#   Python 3.12 base image for arm64. Lambda runs `handler.lambda_handler` every time Cognito issues
#   tokens (sign-in and refresh). For a user who signed in through Microsoft Entra ID, the function
#   turns their Entra app roles (AppRole-ShopAssist-*, or Entra group ids in the fallback mode) into
#   one ShopAssist group (Group-ShopAssist-*); Cognito's own users pass through unchanged.
#
# Where it fits
#   Built only by GitHub Actions on a native arm64 runner (ci.yml / build.yml build, check and scan it;
#   deploy.yml pushes it to the ECR repository shopassist-role-mapper, tagged with the git SHA).
#   The image is always built and pushed, but infra/terraform/modules/role_mapper creates the function
#   only when enable_entra = true (Entra sign-in is off by default). Cognito then calls it just before
#   it issues tokens (the pre-token-generation trigger).
#
# Step by step
#   1. Start from public.ecr.aws/lambda/python:3.12-arm64 (it already contains the Lambda runtime).
#   2. Copy handler.py into the task root.
#   3. CMD names the function Lambda calls.
#
# Build context: the REPO ROOT, like the other images; identity/role_mapper/Dockerfile.dockerignore
# limits what the root context sends to this one file.
#
# Key terms
#   Task root: the folder Lambda imports the handler from (LAMBDA_TASK_ROOT).
#   Cognito: AWS's user sign-in service; it issues the tokens that say who a user is and their groups.
#   Pre-token-generation trigger: a Lambda that Cognito calls to adjust a token before handing it out.
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
COPY identity/role_mapper/handler.py ${LAMBDA_TASK_ROOT}/

# Step 3: module.function that Lambda calls for every event.
CMD ["handler.lambda_handler"]
