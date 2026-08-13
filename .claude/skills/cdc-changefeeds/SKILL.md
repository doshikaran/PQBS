# Skill: CDC and Changefeeds

Use this skill when wiring CockroachDB changefeeds to the screening worker, handling duplicate event delivery, implementing idempotent consumers, or falling back to polling if CDC is unavailable.

---

## What CDC Provides

CockroachDB CDC (Change Data Capture) drives events from the transaction log — every committed insert and update emits an event. This guarantees no committed write escapes screening, because the feed is driven by the log, not by a query that might miss rows.

**From design §14.1:** "This is native to the substrate rather than application polling, which guarantees no committed write escapes screening — the feed is driven by the transaction log, not by a query that might miss rows."

---

## Changefeed Creation

```sql
-- [VERIFY] Syntax against current CockroachDB docs for your cluster tier
-- Webhook sink (triggers Lambda on each event batch)
CREATE CHANGEFEED FOR TABLE belief
  INTO 'webhook-https://<lambda-url>?insecure_tls_skip_verify=false'
  WITH
    updated,           -- includes before/after row state
    resolved = '10s',  -- progress heartbeat every 10s
    envelope = wrapped,-- wraps events with metadata
    format = json;
```

**Verify V2 findings before running this.** The Phase 0 V2 spike confirms CDC is available on the cluster tier and measures cost.

---

## Event Format (envelope=wrapped)

Each event delivered to the sink:

```json
{
  "payload": [
    {
      "after": {
        "belief_id": "...",
        "tenant_id": "...",
        "status": "pending",
        "object": "overnight",
        ...
      },
      "before": null,           // null for inserts
      "key": ["tenant_id_val", "belief_id_val"],
      "topic": "belief",
      "updated": "1678901234.567890000",
      "resolved": null
    }
  ],
  "length": 1
}
```

Parse this into the `ChangeEvent` contract. The `after` field must contain the full row (not just PKs) to support signal evaluation.

---

## Idempotency (Mandatory)

Changefeeds deliver at-least-once. The same event may arrive multiple times. The screening worker must produce the same outcome for repeated events:

```python
def process_change_event(event: ChangeEvent, conn) -> None:
    # Check if this belief has already been screened by this screener version
    existing = conn.execute(
        "SELECT verdict_id FROM integrity_verdict WHERE belief_id = %s AND screener_version = %s",
        [event.belief_id, SCREENER_VERSION]
    ).first()

    if existing is not None:
        # Already screened; skip — do NOT write a duplicate verdict
        log.info("duplicate_event_skipped", belief_id=event.belief_id)
        return

    # Proceed with screening
    verdict = run_screening(event, conn)
    write_verdict(verdict, conn)
```

Key idempotency on `(belief_id, screener_version)` for initial screening. Re-screening (triggered by cascade or version upgrade) uses a different `re_screen_reason` field and is intentionally repeatable.

---

## Polling Fallback (if V2 finds CDC unavailable)

If the V2 spike finds CDC unavailable on the cluster tier, fall back to polling:

```python
async def polling_screener(poll_interval_seconds: int = 5) -> None:
    while True:
        pending = conn.execute(
            """SELECT * FROM belief
               WHERE status = 'pending'
               AND screened_at IS NULL
               ORDER BY tx_from
               LIMIT 100"""
        ).fetchall()

        for belief_row in pending:
            event = ChangeEvent(
                belief_id=belief_row['belief_id'],
                tenant_id=belief_row['tenant_id'],
                operation='insert',
                before=None,
                after=dict(belief_row),
                commit_timestamp=belief_row['tx_from']
            )
            process_change_event(event, conn)

        await asyncio.sleep(poll_interval_seconds)
```

**Disclosure requirement:** if using polling, the README must state: "CDC was unavailable on the free-tier cluster. The polling fallback weakens the guarantee from 'no committed write escapes screening' to 'no write escapes screening within the 5-second poll interval.'" Do not ship a polling loop while claiming log-driven guarantees.

---

## Cost Management

A changefeed running continuously is the most likely way to exhaust the free-tier allowance.

**Add a teardown step to every test session:**
```bash
# [VERIFY] Exact syntax for listing and canceling jobs
SHOW CHANGEFEED JOBS;
CANCEL JOB <job_id>;
```

Or use a teardown script:
```bash
#!/bin/bash
# scripts/teardown_changefeeds.sh
cockroach sql --url "$COCKROACH_URL" -e "CANCEL JOB (SELECT job_id FROM [SHOW CHANGEFEED JOBS] WHERE status = 'running')"
```

---

## Fail-Closed Behavior

When the screening worker is down:
- CDC events may buffer in the sink (webhook/S3)
- Beliefs accumulate in `pending` state
- `role_consumer` retrieval returns zero results (no trusted beliefs being added)

This is correct behavior, not a failure mode. The fail-closed test verifies this explicitly:

```python
def test_fail_closed():
    stop_screening_worker()
    belief_ids = [write_belief() for _ in range(10)]
    assert all(get_status(id) == 'pending' for id in belief_ids)
    with connect_as('role_consumer') as conn:
        assert len(recall(conn, "anything")) == 0
    start_screening_worker()
    wait_for_screening(belief_ids, timeout=30)
```

---

## Resolved Timestamps

Changefeeds emit `resolved` timestamps periodically. These provide a progress guarantee: all events with `updated < resolved` have been delivered.

Use resolved timestamps to detect CDC lag:
```python
def monitor_cdc_lag(event):
    if event.get('resolved'):
        lag = datetime.utcnow() - parse_crdb_timestamp(event['resolved'])
        metrics.record('cdc_lag_seconds', lag.total_seconds())
        if lag.total_seconds() > ALERT_THRESHOLD:
            alert('cdc_lag_exceeded', lag=lag)
```

---

## Resumption After Worker Restart

When the screening worker restarts, events buffered during downtime are delivered. The idempotency check handles any duplicates from the restart boundary. No special restart logic is needed beyond the standard idempotency pattern.
