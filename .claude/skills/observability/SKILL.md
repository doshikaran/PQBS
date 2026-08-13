# Skill: Observability and Telemetry

Use this skill when implementing A17 telemetry, instrumenting metrics, setting up structured logging with structlog, or building traces across the belief lifecycle.

---

## Four Metric Families (Design §23)

### Health (Performance Signals)

```python
metrics = {
    'write_latency_p50_ms': ...,    # target < 400ms
    'write_latency_p99_ms': ...,    # target < 1200ms
    'screening_lag_p50_ms': ...,    # target < 5000ms (5s)
    'screening_lag_p99_ms': ...,    # target < 15000ms (15s)
    'recall_latency_p50_ms': ...,   # target < 600ms
    'retry_rate_percent': ...,      # target < 5% normal, > 30% under contention test
    'cdc_lag_seconds': ...,         # alert if > threshold
}
```

### Integrity (Screening Quality)

```python
metrics = {
    'quarantine_rate_by_reason': {
        'anomalous_embedding': ...,
        'untrusted_source': ...,
        'imperative_content': ...,
        # ...
    },
    'trust_score_distribution': ...,   # histogram
    'inconclusive_rate': ...,
    'rescreen_volume': ...,
    'cascade_depth_p50': ...,
    'cascade_depth_p99': ...,
    'review_queue_depth': ...,
    'review_queue_age_hours': ...,
}
```

### Security (Threat Signals)

```python
metrics = {
    'per_agent_anomaly_score': {agent_id: score, ...},
    'contradiction_burst_rate_by_predicate': ...,
    'quarantine_rate_by_author_agent': ...,
    'imperative_content_detection_rate': ...,
    'federation_rejection_rate': ...,
}
```

### Evaluation (A15 Results)

```python
metrics = {
    'detection_rate_by_class': ...,
    'false_positive_rate': ...,
    'evasion_resistance': ...,
    'regression_delta': ...,   # vs. previous screener version
}
```

---

## The Two Numbers That Matter Most

**Screening lag** — the width of the fail-closed window. Every second here is a second of legitimate knowledge being unusable. Report p50 and p99.

**Retry rate** — the direct cost of serializable isolation. Report it. Do not hide it. A reviewer who asks "what does fail-closed cost you?" must get a number, not a paragraph.

---

## Structured Logging with structlog

```python
# src/pqbs/telemetry/logging.py
import structlog

log = structlog.get_logger()

# Configure once at application startup
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt='iso'),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),  # JSON for CloudWatch
    ]
)
```

Usage throughout the codebase:

```python
log.info("belief_written",
         belief_id=str(belief_id),
         tenant_id=str(tenant_id),
         predicate=predicate,
         write_latency_ms=latency_ms,
         retry_count=retry_count)

log.info("belief_screened",
         belief_id=str(belief_id),
         verdict=verdict,
         trust_score=trust_score,
         screening_lag_ms=lag_ms,
         screener_version=SCREENER_VERSION)

log.warning("cascade_cycle_detected",
            belief_id=str(current_id),
            depth=depth)

log.error("audit_sink_unavailable",
          bucket=WORM_BUCKET,
          error=str(e))
```

---

## Lifecycle Traces

A single belief trace spans: ingestion → canonicalization → embedding → commit → change event → verdict → first retrieval.

Use a correlation ID (`belief_id`) passed through all components:

```python
# Every log entry for a belief includes belief_id
# This allows: SELECT * FROM logs WHERE belief_id = 'uuid' ORDER BY timestamp

log = structlog.get_logger().bind(belief_id=str(belief_id), tenant_id=str(tenant_id))

log.info("ingestion_started", subject=subject, predicate=predicate)
# ... A11 canonicalization ...
log.info("canonicalization_complete", object_normalized=normalized, sensitivity=sensitivity)
# ... A12 embedding ...
log.info("embedding_complete", model=EMBEDDING_MODEL)
# ... transaction opens ...
log.info("transaction_started")
# ... A7 resolution ...
log.info("resolution_complete", resolution=resolution, retry_count=retry_count)
log.info("belief_committed", status='pending')
# ... CDC fires ...
log.info("screening_started", screener_version=SCREENER_VERSION)
# ... signals ...
log.info("screening_complete", verdict=verdict, trust_score=trust_score, lag_ms=lag_ms)
# ... first recall ...
log.info("first_recall", retrieval_id=str(retrieval_id))
```

---

## Screening Lag Measurement

```python
def measure_screening_lag(belief_id: UUID, conn) -> int:
    """Returns lag in milliseconds from commit to verdict."""
    row = conn.execute(
        """SELECT
               EXTRACT(EPOCH FROM (iv.screened_at - b.tx_from)) * 1000 AS lag_ms
           FROM belief b
           JOIN integrity_verdict iv ON b.belief_id = iv.belief_id
           WHERE b.belief_id = %s
           ORDER BY iv.screened_at
           LIMIT 1""",
        [belief_id]
    ).first()
    return int(row['lag_ms']) if row else None
```

Alert if p99 exceeds 15 seconds:
```python
if lag_p99 > 15_000:
    log.warning("screening_lag_alert",
                p99_ms=lag_p99,
                threshold_ms=15_000)
```

---

## A17 Telemetry Agent

A17 aggregates metrics from all agents. Runs as a periodic background task:

```python
# src/pqbs/agents/platform/a17_telemetry.py
import asyncio
from datetime import datetime, timedelta

async def collect_metrics(conn, interval_seconds: int = 60) -> None:
    while True:
        window = datetime.utcnow() - timedelta(seconds=interval_seconds)

        # Health metrics
        write_latency = measure_write_latency_percentiles(conn, since=window)
        screening_lag = measure_screening_lag_percentiles(conn, since=window)
        retry_rate = measure_retry_rate(conn, since=window)

        # Integrity metrics
        quarantine_distribution = measure_quarantine_by_reason(conn, since=window)
        cascade_depth = measure_cascade_depth_percentiles(conn, since=window)

        emit_metrics({
            'write_latency_p50_ms': write_latency.p50,
            'write_latency_p99_ms': write_latency.p99,
            'screening_lag_p50_ms': screening_lag.p50,
            'screening_lag_p99_ms': screening_lag.p99,
            'retry_rate_percent': retry_rate * 100,
            'cascade_depth_p99': cascade_depth.p99,
            **quarantine_distribution,
        })

        await asyncio.sleep(interval_seconds)
```

---

## Demo Dashboard (Minimal)

Show these on the demo UI's telemetry panel:
- Current screening lag (live, updates every 5s)
- Write path latency p50
- Retry rate (normal vs. contention test mode)
- Quarantine rate by reason code
- Review queue depth

These numbers make the "fail-closed costs you X seconds per belief" claim visible in real time during the demo.

---

## CloudWatch Integration (AWS)

```python
import boto3

cloudwatch = boto3.client('cloudwatch', region_name=os.environ['AWS_REGION'])

def emit_metrics(metrics: dict) -> None:
    cloudwatch.put_metric_data(
        Namespace='PQBS',
        MetricData=[
            {
                'MetricName': name,
                'Value': value,
                'Unit': 'Milliseconds' if 'ms' in name else 'Percent' if 'percent' in name else 'Count',
                'Timestamp': datetime.utcnow(),
            }
            for name, value in metrics.items()
        ]
    )
```

Set up CloudWatch alarms for:
- Screening lag p99 > 15s
- CDC lag > 30s
- Retry rate > 50% (indicates hot-key problem)
- Review queue depth > 100 items unattended > 1 hour
