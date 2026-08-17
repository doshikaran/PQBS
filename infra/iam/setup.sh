#!/usr/bin/env bash
# Provision IAM resources for PQBS.
#
# Creates:
#   1. pqbs-app IAM user with pqbs-app-runtime-policy (S3 write + Bedrock).
#   2. pqbs-screener IAM role (Lambda execution role) with pqbs-screener-lambda-role-policy.
#
# Prerequisite: AWS CLI configured with AdministratorAccess or equivalent.
#
# Usage:
#   AWS_REGION=us-east-1 AWS_ACCOUNT_ID=123456789012 ./infra/iam/setup.sh
#
# Outputs:
#   - Access key for pqbs-app (add to .env as AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)
#   - Role ARN for pqbs-screener (add to PQBS_SCREENER_ROLE_ARN before running deploy.sh)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
REGION="${AWS_REGION:-us-east-1}"

APP_USER="pqbs-app"
APP_POLICY_NAME="pqbs-app-runtime-policy"
SCREENER_ROLE="pqbs-screener"
SCREENER_POLICY_NAME="pqbs-screener-lambda-role-policy"

echo "==> Account: ${ACCOUNT_ID}, Region: ${REGION}"

# ------------------------------------------------------------------
# 1. pqbs-app IAM user
# ------------------------------------------------------------------
echo "==> Creating IAM user: ${APP_USER}"
aws iam create-user --user-name "${APP_USER}" 2>/dev/null \
  && echo "    Created." || echo "    Already exists — skipping."

echo "==> Attaching inline policy: ${APP_POLICY_NAME}"
aws iam put-user-policy \
  --user-name "${APP_USER}" \
  --policy-name "${APP_POLICY_NAME}" \
  --policy-document "file://${SCRIPT_DIR}/pqbs-app-runtime-policy.json"

echo "==> Creating access key for ${APP_USER}"
KEY_OUTPUT=$(aws iam create-access-key --user-name "${APP_USER}" --output json)
ACCESS_KEY_ID=$(echo "${KEY_OUTPUT}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['AccessKey']['AccessKeyId'])")
SECRET_KEY=$(echo "${KEY_OUTPUT}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['AccessKey']['SecretAccessKey'])")

echo ""
echo "==> pqbs-app credentials (add to .env — NEVER commit):"
echo "    AWS_ACCESS_KEY_ID=${ACCESS_KEY_ID}"
echo "    AWS_SECRET_ACCESS_KEY=${SECRET_KEY}"
echo ""

# ------------------------------------------------------------------
# 2. pqbs-screener Lambda execution role
# ------------------------------------------------------------------
echo "==> Creating IAM role: ${SCREENER_ROLE}"
ROLE_ARN=$(aws iam create-role \
  --role-name "${SCREENER_ROLE}" \
  --assume-role-policy-document "file://${SCRIPT_DIR}/pqbs-screener-trust-policy.json" \
  --description "Lambda execution role for PQBS screening worker" \
  --output json 2>/dev/null \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['Role']['Arn'])") \
  || ROLE_ARN=$(aws iam get-role --role-name "${SCREENER_ROLE}" --query 'Role.Arn' --output text)

echo "    Role ARN: ${ROLE_ARN}"

echo "==> Attaching inline policy: ${SCREENER_POLICY_NAME}"
aws iam put-role-policy \
  --role-name "${SCREENER_ROLE}" \
  --policy-name "${SCREENER_POLICY_NAME}" \
  --policy-document "file://${SCRIPT_DIR}/pqbs-screener-lambda-role-policy.json"

echo ""
echo "==> PQBS_SCREENER_ROLE_ARN=${ROLE_ARN}"
echo "    Export this before running infra/lambda/deploy.sh:"
echo "    export PQBS_SCREENER_ROLE_ARN=${ROLE_ARN}"
echo ""
echo "==> Done."
