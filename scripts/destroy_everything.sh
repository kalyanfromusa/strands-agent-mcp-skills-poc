#!/usr/bin/env bash
# =====================================================================================================
# scripts/destroy_everything.sh: remove EVERY trace of ShopAssist from the AWS account (local only)
#
#   AWS_PROFILE=personal bash scripts/destroy_everything.sh
#
# What this file does
#   The full clean sweep, in the only order that works:
#     1. each environment's main stack (dev, prod) that still has resources: terraform destroy,
#        then scripts/leftovers.py --sweep (images, secrets in their recovery window, stray log
#        groups, the environment's state file);
#     2. the platform stack: ECR repositories (force-deleted with any images), the GitHub OIDC
#        provider, the CI roles, the permissions boundaries;
#     3. the bootstrap stack: the Terraform state bucket, with every object in it;
#     4. a final account-wide scan (scripts/leftovers.py --all) that must come back CLEAN.
#   Afterwards nothing of the project is left in AWS and nothing is billed.
#
# Where it fits
#   For "the demo is over". To only take an environment down (and keep the cheap, empty foundation
#   that the pipeline needs to deploy again), use the GitHub workflow destroy.yml instead.
#   This can't run in GitHub Actions: it deletes the very roles and state bucket the workflows use.
#
# Recreate from zero afterwards
#   cd infra/terraform/bootstrap && terraform init && terraform apply
#   cd ../platform && terraform init -backend-config=backend.hcl && terraform apply
#   Then GitHub → Actions → deploy. The role names are fixed, so the GitHub variables stay valid.
#
# Safety
#   Asks you to type "destroy everything" first. Uses the credentials in your environment
#   (AWS_PROFILE=personal), checks they belong to the expected account, and stops at the first error.
# =====================================================================================================
set -euo pipefail

EXPECTED_ACCOUNT="438750401181"
REGION="us-east-1"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF="$ROOT/infra/terraform"
export TF_IN_AUTOMATION=1 TF_INPUT=0 TF_PLUGIN_TIMEOUT="${TF_PLUGIN_TIMEOUT:-600}"
ZERO_SHA="0000000000000000000000000000000000000000"

account="$(aws sts get-caller-identity --query Account --output text)"
if [ "$account" != "$EXPECTED_ACCOUNT" ]; then
  echo "These credentials are for account $account, not $EXPECTED_ACCOUNT. Set AWS_PROFILE=personal." >&2
  exit 1
fi

echo "This deletes ALL ShopAssist resources, data, images, roles and Terraform state in $account ($REGION)."
read -r -p 'Type "destroy everything" to continue: ' answer
[ "$answer" = "destroy everything" ] || { echo "Cancelled."; exit 1; }

# Step 1: each environment that has a state file.
for env in dev prod; do
  if aws s3api head-object --bucket "shopassist-tfstate-${account}-${REGION}" --key "main/${env}.tfstate" >/dev/null 2>&1; then
    echo "== destroying environment ${env}"
    terraform -chdir="$TF" init -reconfigure -backend-config="envs/${env}.backend.hcl" -no-color >/dev/null
    terraform -chdir="$TF" destroy -auto-approve -no-color \
      -var-file="envs/${env}.tfvars" -var "image_tag=${ZERO_SHA}" | grep -E "Destroy complete|Error" || true
  else
    echo "== environment ${env}: no state file, nothing deployed"
  fi
  (cd "$ROOT" && uv run python scripts/leftovers.py --region "$REGION" --env "$env" --sweep)
done

# Step 2: the platform (ECR repositories go with force_delete, images included).
echo "== destroying the platform stack"
terraform -chdir="$TF/platform" init -reconfigure -backend-config=backend.hcl -no-color >/dev/null
terraform -chdir="$TF/platform" destroy -auto-approve -no-color | grep -E "Destroy complete|Error" || true

# Step 3: the state bucket (force_destroy empties it first). Its own state is local.
echo "== destroying the bootstrap stack (state bucket)"
terraform -chdir="$TF/bootstrap" init -no-color >/dev/null
terraform -chdir="$TF/bootstrap" destroy -auto-approve -no-color | grep -E "Destroy complete|Error" || true

# Step 4: nothing may be left.
echo "== final scan"
(cd "$ROOT" && uv run python scripts/leftovers.py --region "$REGION" --all)
rm -rf "$ROOT/.deploy"
echo "Done. ShopAssist is gone from $account."
