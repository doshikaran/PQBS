# Skill: AWS Services for PQBS

Use this skill when configuring AWS Lambda for the screening worker, S3 Object Lock for WORM audit, Amazon Bedrock for model inference and embedding, CloudWatch for observability, Secrets Manager for agent credentials, or managing IAM roles and cost discipline.

---

## Five Services Used in PQBS (Submission Requirement)

All five must be identified explicitly in the README submission:

| Service | Use | Owner |
|---|---|---|
| Amazon Bedrock | Foundation models (A1, A4 S3 signal, A9) + Embedding model (A12) | E2, E4 |
| AWS Lambda | Screening worker (CDC-triggered), A18 posture verification (scheduled), A19 substrate custody (scheduled) | E2, E3 |
| Amazon S3 (Object Lock) | WORM immutable audit sink — belief-layer and substrate-layer records | E3 |
| AWS Secrets Manager | Agent credential storage (referenced by agent_identity.credential_ref); A19 service account credentials | E1, E3 |
| Amazon CloudWatch | Metrics, logs, alarms (A17 telemetry); Lambda invocation monitoring for A18/A19 | E5 |

**IAM** is infrastructure rather than a billable service, but is also required: database role credentials, Lambda execution roles, S3 access policies, A19 service account RBAC.

---

## Lambda — Three Workers

Lambda hosts three workers in PQBS:

1. **Screening worker** (E2) — CDC-triggered, processes each new belief
2. **A18 posture-verification worker** (E3) — scheduled, verifies schema posture against baseline
3. **A19 substrate-custody worker** (E3) — scheduled, polls ccloud for control-plane audit events and backup state

### Screening Worker (CDC-triggered)

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

**Screening Lambda configuration:**
- Timeout: 30 seconds (screening may involve model calls)
- Memory: 512 MB
- Concurrency: reserve enough to handle burst CDC events without throttling
- Environment variables: `COCKROACH_URL`, `AWS_REGION`, `BEDROCK_MODEL_ID`, `WORM_BUCKET`, `SCREENER_VERSION`

### A18 Posture-Verification Worker (Scheduled)

Runs on a CloudWatch Events / EventBridge schedule (e.g., every 15 minutes). Invokes A18's posture-check loop against the live schema catalog and writes attestation or drift records to the WORM sink.

```python
# infra/lambda/posture_verifier/handler.py
import json
from pqbs.agents.integrity.a18_posture import verify_posture
from pqbs.substrate.connection import get_connection
import boto3

def lambda_handler(event, context):
    with get_connection() as conn:
        result = verify_posture(conn)
    return {'statusCode': 200, 'body': json.dumps({'status': result.status})}
```

**A18 Lambda configuration:**
- Timeout: 60 seconds
- Memory: 256 MB
- Trigger: CloudWatch Events schedule (every 15 minutes)
- IAM role: read-only on CRDB schema catalog; `s3:PutObject` on WORM bucket; no DDL/GRANT authority
- Environment variables: `COCKROACH_URL`, `WORM_BUCKET`, `POSTURE_BASELINE_PATH`

### A19 Substrate-Custody Worker (Scheduled)

Runs on a schedule. Polls ccloud CLI for new control-plane audit events and backup state changes, ingests them to the WORM sink, and updates the local backup catalog for Mechanism 3.

```python
# infra/lambda/substrate_custody/handler.py
import json
from pqbs.agents.integrity.a19_custody import poll_and_ingest

def lambda_handler(event, context):
    result = poll_and_ingest()
    return {'statusCode': 200, 'body': json.dumps({'ingested': result.event_count})}
```

**A19 Lambda configuration:**
- Timeout: 120 seconds (ccloud CLI calls may be slow)
- Memory: 256 MB
- Trigger: CloudWatch Events schedule (configurable; default every 5 minutes)
- IAM role: `s3:PutObject` on WORM bucket; `secretsmanager:GetSecretValue` for ccloud service account credentials; no database write access
- Environment variables: `WORM_BUCKET`, `CCLOUD_SERVICE_ACCOUNT_SECRET_ARN`, `CLUSTER_NAME`, `LAST_POLL_TIMESTAMP_PARAM`

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

In the README's "AWS services used" section, be specific about all five:

1. **Amazon Bedrock** — used for embedding computation (A12: both write-path and query-path, same model to ensure recall coherence) and for S3 imperative-content classification (A4 signal S3 uses claude-3-haiku to classify object text as assertion vs. instruction).

2. **AWS Lambda** — used for three workers: (a) CDC-triggered screening worker (A4); (b) scheduled posture-verification worker (A18, every 15 minutes); (c) scheduled substrate-custody worker (A19, polls ccloud for control-plane audit events and backup state). All three are stateless and independently scalable.

3. **Amazon S3 with Object Lock (COMPLIANCE mode)** — used as the WORM audit sink for both audit layers. Belief-layer records (every state transition) and substrate-layer records (control-plane events via A19) are written under distinct key prefixes. Delete attempts fail at the storage level regardless of application-level compromise.

4. **AWS Secrets Manager** — used to store agent credentials (CockroachDB connection URLs per role) and the A19 ccloud service account credentials. `agent_identity.credential_ref` holds the Secrets Manager ARN, not the credential itself.

5. **Amazon CloudWatch** — used by A17 (Telemetry) for metrics, structured logs, and alarms across all four metric families (health, integrity, security, evaluation). Also used for EventBridge schedules that trigger A18 and A19 Lambda workers.
