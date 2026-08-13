# Skill: AWS Services for PQBS

Use this skill when configuring AWS Lambda for the screening worker, S3 Object Lock for WORM audit, Amazon Bedrock for model inference and embedding, or managing IAM roles and cost discipline.

---

## Services Used in PQBS

| Service | Use | Owner |
|---|---|---|
| AWS Lambda | Screening worker (CDC-triggered), cascade worker | E2, E3 |
| Amazon S3 (Object Lock) | WORM immutable audit sink | E3 |
| Amazon Bedrock | Foundation models (A1, A4 S3 signal, A9) + Embedding model (A12) | E2, E4 |
| AWS IAM | Database role credentials, Lambda execution roles, S3 access | E3 |
| Amazon CloudWatch | Metrics, logs, alarms (A17) | E5 |
| AWS Secrets Manager | Agent credential storage (referenced by agent_identity.credential_ref) | E1 |

---

## Lambda — Screening Worker

The screening worker receives CDC events (webhook trigger) and processes each belief:

```python
# infra/lambda/screener/handler.py
import json
import os
from pqbs.integrity.screening import screen_belief
from pqbs.contracts import ChangeEvent
from pqbs.substrate.connection import get_connection

def lambda_handler(event, context):
    # CockroachDB changefeed webhook delivers events in batches
    body = json.loads(event.get('body', '[]'))
    processed = 0

    with get_connection() as conn:
        for raw_event in body.get('payload', []):
            if raw_event.get('resolved'):
                continue   # skip resolved timestamps

            change_event = ChangeEvent.model_validate({
                'belief_id': raw_event['after']['belief_id'],
                'tenant_id': raw_event['after']['tenant_id'],
                'operation': 'insert' if raw_event['before'] is None else 'update',
                'before': raw_event.get('before'),
                'after': raw_event['after'],
                'commit_timestamp': raw_event['updated'],
            })

            screen_belief(change_event, conn)
            processed += 1

    return {'statusCode': 200, 'body': json.dumps({'processed': processed})}
```

**Lambda configuration:**
- Timeout: 30 seconds (screening may involve model calls)
- Memory: 512 MB
- Concurrency: reserve enough to handle burst CDC events without throttling
- Environment variables: `COCKROACH_URL`, `AWS_REGION`, `BEDROCK_MODEL_ID`, `WORM_BUCKET`, `SCREENER_VERSION`

---

## S3 Object Lock (WORM Audit Sink)

See `audit-worm` skill for full configuration. Key reminder:

```bash
# NEVER use --object-lock-enabled-for-bucket on the dev bucket
# Only the production audit bucket gets COMPLIANCE mode retention

# Dev bucket (no lock):
aws s3api create-bucket --bucket pqbs-audit-dev --region us-east-1

# Production WORM bucket (COMPLIANCE, 365 days):
aws s3api create-bucket \
  --bucket pqbs-audit-prod \
  --region us-east-1 \
  --object-lock-enabled-for-bucket
```

---

## Amazon Bedrock — Foundation Models and Embeddings

```python
# src/pqbs/agents/semantics/embedding.py (E1 owns this function)
import boto3
import json
import os

bedrock = boto3.client('bedrock-runtime', region_name=os.environ['AWS_REGION'])

EMBEDDING_MODEL_ID = os.environ['BEDROCK_EMBEDDING_MODEL_ID']  # e.g., amazon.titan-embed-text-v2:0

def compute_embedding(text: str) -> list[float]:
    """
    Single shared function for write path AND recall path.
    Both paths MUST call this function — not separate calls with different configs.
    """
    response = bedrock.invoke_model(
        modelId=EMBEDDING_MODEL_ID,
        body=json.dumps({'inputText': text}),
        contentType='application/json',
        accept='application/json'
    )
    body = json.loads(response['body'].read())
    return body['embedding']   # [VERIFY] exact response field for your model

# [PIN-ON-INSTALL] Pin the exact model ID and embedding dimensions in requirements.txt comment
# EMBEDDING_DIMENSIONS = 1536  # example — capture actual from V1 spike
```

**For model inference (S3 signal classification, A1 extraction):**
```python
# [VERIFY] Exact API for your chosen model (claude-3-haiku, llama3, etc.)
response = bedrock.invoke_model(
    modelId=os.environ['BEDROCK_MODEL_ID'],
    body=json.dumps({
        'anthropic_version': 'bedrock-2023-05-31',
        'max_tokens': 200,
        'messages': [{'role': 'user', 'content': prompt}]
    }),
    contentType='application/json',
    accept='application/json'
)
```

**Cache embeddings during development:**
```python
import hashlib
import pickle
from pathlib import Path

_cache = {}

def compute_embedding_cached(text: str) -> list[float]:
    key = hashlib.sha256(text.encode()).hexdigest()
    if key in _cache:
        return _cache[key]
    emb = compute_embedding(text)
    _cache[key] = emb
    return emb
```

Only use the cache during development/seeding, not in production screening.

---

## IAM Roles and Policies

```json
// infra/iam/screener-lambda-policy.json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel"
      ],
      "Resource": "arn:aws:bedrock:*::foundation-model/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject"
      ],
      "Resource": "arn:aws:s3:::pqbs-audit-prod/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Resource": "arn:aws:secretsmanager:*:*:secret:pqbs/*"
    }
  ]
}
```

Lambda execution role must NOT have:
- `s3:DeleteObject` (WORM enforcement)
- Any write access to production belief tables (CRDB access via connection string only)

---

## Secrets Manager (Agent Credentials)

```python
import boto3

secrets = boto3.client('secretsmanager', region_name=os.environ['AWS_REGION'])

def get_db_url() -> str:
    secret = secrets.get_secret_value(SecretId='pqbs/cockroach-url')
    return secret['SecretString']
```

`agent_identity.credential_ref` stores the Secrets Manager ARN, not the credential itself. Never put credentials in environment variables for production deployments — use Secrets Manager.

---

## Cost Discipline

The two biggest cost risks:

1. **Bedrock model calls during screening.** Use the lexical prefilter before S3 model calls. Cache embeddings by content hash during development.

2. **CockroachDB changefeed.** Runs continuously. Disable between test sessions.

**Daily check:**
```bash
# Check Lambda invocations (proxy for screening volume)
aws cloudwatch get-metric-statistics \
  --namespace AWS/Lambda \
  --metric-name Invocations \
  --dimensions Name=FunctionName,Value=pqbs-screener \
  --start-time $(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 86400 \
  --statistics Sum

# Check Bedrock token usage
# [VERIFY] Bedrock-specific cost monitoring via AWS Cost Explorer
```

Update `docs/BUDGET.md` daily with actual consumption vs. free-tier allowance.

---

## Submission Claims

In the README's "AWS services used" section, be specific:

1. **Amazon Bedrock** — used for embedding computation (A12: both write-path and query-path, same model to ensure recall coherence) and for S3 imperative-content classification (A4 signal S3 uses claude-3-haiku to classify object text as assertion vs. instruction).

2. **Amazon S3 with Object Lock (COMPLIANCE mode)** — used as the WORM audit sink. Every belief state transition emits an immutable audit record; delete attempts on audit objects fail at the storage level regardless of application-level compromise.

3. **AWS Lambda** — used as the CDC-triggered screening worker. Stateless, scales with write volume, invoked by the CockroachDB changefeed webhook sink.
