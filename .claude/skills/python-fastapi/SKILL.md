# Skill: Python and FastAPI Patterns for PQBS

Use this skill when structuring Python code, designing FastAPI endpoints, using Pydantic v2 models, or setting up async patterns for the screening worker and recall API.

---

## Project Python Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Core dependencies:
- `psycopg[binary]` — PostgreSQL wire driver for CockroachDB
- `sqlalchemy` — optional ORM (raw SQL preferred for clarity)
- `alembic` — migrations
- `boto3` — AWS SDK
- `pydantic` — interface contracts
- `python-dotenv` — environment variable loading
- `structlog` — structured logging
- `pytest pytest-asyncio` — testing
- `numpy` — embedding math for signal S1
- `fastapi uvicorn` — demo API surface

---

## Pydantic v2 Contract Patterns

```python
from pydantic import BaseModel, Field, model_validator
from typing import Literal, Optional
from uuid import UUID, uuid4
from datetime import datetime

class BeliefWriteRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=500)
    predicate: str = Field(min_length=1, max_length=200)
    object: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    valid_from: datetime
    valid_to: Optional[datetime] = None
    author_agent_id: str = Field(min_length=1)

    class Config:
        frozen = True   # contracts are immutable

    @model_validator(mode='after')
    def validate_validity_window(self) -> 'BeliefWriteRequest':
        if self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        return self
```

---

## FastAPI Application Structure

```python
# src/pqbs/main.py
from fastapi import FastAPI, Depends, HTTPException
from contextlib import asynccontextmanager
import psycopg
import os

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: establish DB connection pool
    app.state.db_pool = await create_async_pool(os.environ['COCKROACH_URL'])
    yield
    # Shutdown: close pool
    await app.state.db_pool.close()

app = FastAPI(title="PQBS API", lifespan=lifespan)

# Route groups
from pqbs.recall.router import router as recall_router
from pqbs.audit.router import router as audit_router
from pqbs.review.router import router as review_router

app.include_router(recall_router, prefix="/recall")
app.include_router(audit_router, prefix="/audit")
app.include_router(review_router, prefix="/review")
```

---

## DB Connection Pattern

```python
# src/pqbs/substrate/connection.py
import psycopg
import os
from contextlib import contextmanager

def get_connection(role: str | None = None) -> psycopg.Connection:
    url = os.environ['COCKROACH_URL']
    # Append role to connection options if specified
    conn = psycopg.connect(url, options=f"-c role={role}" if role else "")
    return conn

@contextmanager
def get_consumer_connection():
    with get_connection(role='role_consumer') as conn:
        yield conn

@contextmanager
def get_producer_connection():
    with get_connection(role='role_producer') as conn:
        yield conn
```

---

## Async Screening Worker

The screening worker runs as an async loop (or Lambda handler — see aws-services skill):

```python
# infra/lambda/screener/worker.py
import asyncio
from pqbs.integrity.screening import screen_belief
from pqbs.contracts import ChangeEvent
from pqbs.telemetry.logging import log

async def process_event(event_raw: dict, conn) -> None:
    try:
        event = ChangeEvent.model_validate(event_raw)
        await screen_belief(event, conn)
        log.info("screening_complete", belief_id=str(event.belief_id))
    except Exception as e:
        log.error("screening_error", belief_id=str(event_raw.get('belief_id')), error=str(e))
        # Do NOT raise — keep worker alive for next event
        # Belief stays in pending state (fail-closed)
```

---

## Environment Variable Loading

```python
# At application entry point
from dotenv import load_dotenv
load_dotenv()   # reads .env (git-ignored) or .env.example as template

# Required variables — fail fast if missing
required = ['COCKROACH_URL', 'AWS_REGION', 'BEDROCK_MODEL_ID',
            'BEDROCK_EMBEDDING_MODEL_ID', 'WORM_BUCKET', 'SCREENER_VERSION']
for var in required:
    if not os.environ.get(var):
        raise RuntimeError(f"Required environment variable {var} is not set")
```

---

## Recall API Endpoint

```python
# src/pqbs/recall/router.py
from fastapi import APIRouter, Depends
from pqbs.contracts import RecallRequest, RecallResult
from pqbs.recall.service import recall_beliefs
from pqbs.substrate.connection import get_consumer_connection

router = APIRouter()

@router.post("/", response_model=RecallResult)
async def recall(request: RecallRequest) -> RecallResult:
    with get_consumer_connection() as conn:
        return await recall_beliefs(request, conn)
```

```python
# src/pqbs/recall/service.py
from pqbs.agents.semantics.embedding import compute_embedding
from pqbs.contracts import RecallRequest, RecallResult
from pqbs.telemetry.logging import log
import time

async def recall_beliefs(request: RecallRequest, conn) -> RecallResult:
    start = time.monotonic()

    # Embed query using the SAME function as write path
    query_embedding = compute_embedding(request.query)

    # Vector search via role-scoped view (filtering is structural, not conditional)
    rows = conn.execute(
        """SELECT b.*, p.source_type, p.source_uri, p.source_trust_tier
           FROM trusted_current_beliefs b
           JOIN provenance p ON b.provenance_id = p.provenance_id
           WHERE b.tenant_id = %s
             AND (%s IS NULL OR b.valid_from <= %s)
             AND (%s IS NULL OR b.valid_to IS NULL OR b.valid_to >= %s)
           ORDER BY b.embedding <-> %s
           LIMIT %s""",
        [request.tenant_id,
         request.temporal_context, request.temporal_context,
         request.temporal_context, request.temporal_context,
         query_embedding, request.limit]
    ).fetchall()

    belief_ids = [r['belief_id'] for r in rows]

    # Log what was actually returned (forensic anchor)
    conn.execute(
        """INSERT INTO retrieval_log (retrieval_id, tenant_id, requesting_agent_id,
                                      query_digest, returned_belief_ids, retrieved_at)
           VALUES (gen_random_uuid(), %s, %s, %s, %s, NOW())""",
        [request.tenant_id, 'a9-recall', hash_query(request.query), belief_ids]
    )
    conn.commit()

    latency_ms = int((time.monotonic() - start) * 1000)
    log.info("recall_complete", tenant_id=str(request.tenant_id),
             count=len(rows), latency_ms=latency_ms)

    return RecallResult(
        beliefs=[dict(r) for r in rows],
        provenance=[],  # populated from join above
        trust_scores=[r['trust_score'] for r in rows],
        retrieval_id=...
    )
```

---

## Error Handling Philosophy

Only validate at system boundaries (user input, external APIs). Trust internal code:

```python
# At API boundary — validate input
@router.post("/beliefs")
async def write_belief(request: BeliefWriteRequest):
    # Pydantic already validated the shape
    # Trust that downstream functions handle their contracts

# Inside internal functions — don't add redundant checks
def resolve_contradiction(incumbent, challenger, policy):
    # Trust that caller passed valid, already-validated objects
    # Don't re-validate fields that Pydantic already checked
    ...
```

---

## Testing with pytest-asyncio

```python
import pytest
import pytest_asyncio

@pytest.mark.asyncio
async def test_recall_returns_only_trusted():
    async with get_test_consumer_conn() as conn:
        result = await recall_beliefs(RecallRequest(
            query="delivery window",
            tenant_id=TEST_TENANT_ID
        ), conn)
        assert all(b['status'] == 'trusted' for b in result.beliefs)
```
