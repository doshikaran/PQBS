#!/usr/bin/env bash
# Provision the PQBS WORM audit bucket on S3.
#
# Object Lock COMPLIANCE mode with 365-day retention is configured at the
# bucket level so application writes (pqbs-app IAM user) do not need
# s3:PutObjectRetention — they write plain objects that inherit the bucket
# default retention. Deletes are blocked by an explicit DENY in the
# pqbs-app-runtime-policy (infra/iam/pqbs-app-runtime-policy.json).
#
# TWO BUCKETS are created:
#   - WORM bucket (production): ObjectLock COMPLIANCE, 365-day retention.
#     Records written here CANNOT be deleted. Do not use for dev/test.
#   - Dev bucket: versioning only, no retention lock.
#     Use PQBS_AUDIT_BUCKET_DEV for all development and test writes.
#
# Usage:
#   AWS_REGION=us-east-1 ./infra/worm/setup.sh
#
# Outputs:
#   PQBS_AUDIT_BUCKET and PQBS_AUDIT_BUCKET_DEV printed at end.
#   Add these to your .env file.

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
SUFFIX="${PQBS_BUCKET_SUFFIX:-$(openssl rand -hex 4)}"

PROD_BUCKET="pqbs-audit-${SUFFIX}"
DEV_BUCKET="pqbs-audit-dev-${SUFFIX}"

echo "==> Creating WORM (production) audit bucket: ${PROD_BUCKET}"
if [[ "${REGION}" == "us-east-1" ]]; then
  # us-east-1 does not accept LocationConstraint
  aws s3api create-bucket \
    --bucket "${PROD_BUCKET}" \
    --object-lock-enabled-for-bucket \
    --region "${REGION}"
else
  aws s3api create-bucket \
    --bucket "${PROD_BUCKET}" \
    --create-bucket-configuration "LocationConstraint=${REGION}" \
    --object-lock-enabled-for-bucket \
    --region "${REGION}"
fi

echo "==> Enabling versioning on ${PROD_BUCKET}"
aws s3api put-bucket-versioning \
  --bucket "${PROD_BUCKET}" \
  --versioning-configuration Status=Enabled \
  --region "${REGION}"

echo "==> Configuring Object Lock default retention (COMPLIANCE, 365 days)"
aws s3api put-object-lock-configuration \
  --bucket "${PROD_BUCKET}" \
  --object-lock-configuration '{
    "ObjectLockEnabled": "Enabled",
    "Rule": {
      "DefaultRetention": {
        "Mode": "COMPLIANCE",
        "Days": 365
      }
    }
  }' \
  --region "${REGION}"

echo "==> Applying bucket policy to block all deletes (belt-and-suspenders)"
PROD_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyDeleteObject",
      "Effect": "Deny",
      "Principal": "*",
      "Action": [
        "s3:DeleteObject",
        "s3:DeleteObjectVersion"
      ],
      "Resource": "arn:aws:s3:::${PROD_BUCKET}/*"
    },
    {
      "Sid": "DenyDeleteBucket",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:DeleteBucket",
      "Resource": "arn:aws:s3:::${PROD_BUCKET}"
    }
  ]
}
EOF
)
aws s3api put-bucket-policy \
  --bucket "${PROD_BUCKET}" \
  --policy "${PROD_POLICY}" \
  --region "${REGION}"

# ------------------------------------------------------------------
# Dev bucket — versioning only, NO retention lock
# ------------------------------------------------------------------
echo "==> Creating dev audit bucket: ${DEV_BUCKET}"
if [[ "${REGION}" == "us-east-1" ]]; then
  aws s3api create-bucket \
    --bucket "${DEV_BUCKET}" \
    --region "${REGION}"
else
  aws s3api create-bucket \
    --bucket "${DEV_BUCKET}" \
    --create-bucket-configuration "LocationConstraint=${REGION}" \
    --region "${REGION}"
fi

echo "==> Enabling versioning on ${DEV_BUCKET}"
aws s3api put-bucket-versioning \
  --bucket "${DEV_BUCKET}" \
  --versioning-configuration Status=Enabled \
  --region "${REGION}"

echo ""
echo "==> Done. Add these to your .env:"
echo "    PQBS_AUDIT_BUCKET=${PROD_BUCKET}"
echo "    PQBS_AUDIT_BUCKET_DEV=${DEV_BUCKET}"
echo ""
echo "==> Verification:"
echo "    # Confirm delete is blocked on the WORM bucket:"
echo "    aws s3 cp /dev/null s3://${PROD_BUCKET}/test-object"
echo "    aws s3 rm s3://${PROD_BUCKET}/test-object  # Should return AccessDenied"
