#!/usr/bin/env bash
# Deploy the PQBS screening Lambda.
#
# Prerequisites:
#   - AWS CLI configured with an IAM user/role that has lambda:CreateFunction,
#     lambda:UpdateFunctionCode, lambda:UpdateFunctionConfiguration,
#     iam:PassRole, and ecr:* (if using container image) permissions.
#   - COCKROACH_URL, AWS_REGION, PQBS_AUDIT_BUCKET set in environment or .env.
#   - The pqbs-screener IAM execution role must already exist (run infra/iam/setup.sh first).
#
# Usage:
#   ./infra/lambda/deploy.sh [--update]
#
#   --update  Update an existing Lambda rather than creating from scratch.
#
# What this script does:
#   1. Installs dependencies into a package/ directory.
#   2. Copies the pqbs source tree into package/.
#   3. Zips the bundle.
#   4. Creates or updates the Lambda function.
#   5. Sets environment variables (from the shell environment).
#   6. Prints the Function URL for use as the CDC changefeed sink.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAMBDA_DIR="${REPO_ROOT}/infra/lambda"
BUILD_DIR="${LAMBDA_DIR}/build"
PACKAGE_DIR="${BUILD_DIR}/package"
ZIP_FILE="${BUILD_DIR}/pqbs-screener.zip"

FUNCTION_NAME="${PQBS_LAMBDA_FUNCTION_NAME:-pqbs-screener}"
RUNTIME="python3.12"
HANDLER="handler.handler"
TIMEOUT=60        # seconds — Bedrock S3 round-trip headroom
MEMORY=512        # MB — embedding + gate fit comfortably

# Execution role created by infra/iam/setup.sh
ROLE_ARN="${PQBS_SCREENER_ROLE_ARN:-}"
if [[ -z "${ROLE_ARN}" ]]; then
  echo "ERROR: PQBS_SCREENER_ROLE_ARN must be set (output of infra/iam/setup.sh)"
  exit 1
fi

UPDATE="${1:-}"

# ------------------------------------------------------------------
# Build
# ------------------------------------------------------------------
echo "==> Cleaning build dir"
rm -rf "${BUILD_DIR}"
mkdir -p "${PACKAGE_DIR}"

echo "==> Installing Python dependencies"
pip install --quiet -r "${LAMBDA_DIR}/requirements.txt" -t "${PACKAGE_DIR}"

echo "==> Copying pqbs source"
cp -r "${REPO_ROOT}/src/pqbs" "${PACKAGE_DIR}/pqbs"
cp "${LAMBDA_DIR}/handler.py" "${PACKAGE_DIR}/handler.py"

echo "==> Creating zip bundle"
cd "${PACKAGE_DIR}"
zip -q -r "${ZIP_FILE}" .
cd "${REPO_ROOT}"

echo "==> Bundle: ${ZIP_FILE} ($(du -sh "${ZIP_FILE}" | cut -f1))"

# ------------------------------------------------------------------
# Lambda create / update
# ------------------------------------------------------------------
ENV_VARS="Variables={\
COCKROACH_URL=${COCKROACH_URL},\
AWS_REGION=${AWS_REGION:-us-east-1},\
PQBS_AUDIT_BUCKET=${PQBS_AUDIT_BUCKET:-},\
BEDROCK_MODEL_ID=${BEDROCK_MODEL_ID:-anthropic.claude-3-5-sonnet-20241022-v2:0},\
BEDROCK_EMBEDDING_MODEL_ID=${BEDROCK_EMBEDDING_MODEL_ID:-amazon.titan-embed-text-v2:0},\
SCREENER_VERSION=${SCREENER_VERSION:-1.0.0},\
LOG_LEVEL=${LOG_LEVEL:-INFO},\
TRUST_THRESHOLD=${TRUST_THRESHOLD:-0.7},\
QUARANTINE_THRESHOLD=${QUARANTINE_THRESHOLD:-0.4}\
}"

if [[ "${UPDATE}" == "--update" ]]; then
  echo "==> Updating Lambda function code"
  aws lambda update-function-code \
    --function-name "${FUNCTION_NAME}" \
    --zip-file "fileb://${ZIP_FILE}" \
    --region "${AWS_REGION:-us-east-1}" \
    --output json | jq -r '.FunctionArn'

  echo "==> Updating Lambda environment"
  aws lambda update-function-configuration \
    --function-name "${FUNCTION_NAME}" \
    --timeout "${TIMEOUT}" \
    --memory-size "${MEMORY}" \
    --environment "${ENV_VARS}" \
    --region "${AWS_REGION:-us-east-1}" \
    --output json | jq -r '.LastUpdateStatus'
else
  echo "==> Creating Lambda function"
  aws lambda create-function \
    --function-name "${FUNCTION_NAME}" \
    --runtime "${RUNTIME}" \
    --handler "${HANDLER}" \
    --role "${ROLE_ARN}" \
    --zip-file "fileb://${ZIP_FILE}" \
    --timeout "${TIMEOUT}" \
    --memory-size "${MEMORY}" \
    --environment "${ENV_VARS}" \
    --region "${AWS_REGION:-us-east-1}" \
    --output json | jq -r '.FunctionArn'

  echo "==> Enabling Function URL (no auth — CockroachDB CDC webhook)"
  FUNCTION_URL=$(aws lambda create-function-url-config \
    --function-name "${FUNCTION_NAME}" \
    --auth-type NONE \
    --region "${AWS_REGION:-us-east-1}" \
    --output json | jq -r '.FunctionUrl')

  # Allow public invocation via Function URL
  aws lambda add-permission \
    --function-name "${FUNCTION_NAME}" \
    --statement-id AllowPublicFunctionUrl \
    --action lambda:InvokeFunctionUrl \
    --principal '*' \
    --function-url-auth-type NONE \
    --region "${AWS_REGION:-us-east-1}" \
    --output json > /dev/null

  echo ""
  echo "==> Lambda Function URL (use as CDC changefeed sink):"
  echo "    ${FUNCTION_URL}screen"
  echo ""
  echo "Create the changefeed:"
  echo "  CREATE CHANGEFEED FOR TABLE belief"
  echo "  INTO 'webhook-${FUNCTION_URL}screen'"
  echo "  WITH updated, full_table_name, format = 'json', min_checkpoint_frequency = '1s';"
fi

echo "==> Done."
