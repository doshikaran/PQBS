# Skill: Temporal Reconstruction

Use this skill when implementing bitemporal queries, MVCC as-of queries, or explaining the difference between the two mechanisms to a human or in the README.

---

## Two Mechanisms — Two Questions

These mechanisms answer different questions and must not be conflated. A knowledgeable reviewer will notice if the README conflates them.

| | Mechanism 1 (Bitemporal) | Mechanism 2 (MVCC) |
|---|---|---|
| **Question** | "What did we *believe* at time T?" | "What *state* did the DB have at instant T?" |
| **Bound** | Unbounded — works arbitrarily far back | Bounded — limited by GC retention window |
| **Data source** | `tx_from` / `tx_to` columns (ordinary data) | MVCC snapshot (database-internal) |
| **What it includes** | Beliefs we currently acknowledge were held at T | Exact committed rows including since-revised |
| **Production use** | **Yes — the durable mechanism** | Short-horizon forensics only |
| **README claim** | Safe to claim arbitrary historical replay | Must state the measured bound |

---

## Mechanism 1 — Bitemporal Query (Unbounded)

Filter on the transaction-time columns:

```sql
-- "What did we believe about the world as of time T?"
SELECT b.*, p.source_type, p.source_uri, p.source_trust_tier
FROM belief b
JOIN provenance p ON b.provenance_id = p.provenance_id
WHERE b.tenant_id = $1
  AND b.tx_from <= $2          -- T: the query timestamp
  AND (b.tx_to IS NULL OR b.tx_to > $2)
ORDER BY b.tx_from;
```

This uses ordinary indexed data. No special DB support required. Works for any timestamp, including years ago.

**Bitemporal semantics:**
- `tx_from` = when the system first held this belief
- `tx_to` = when the system stopped holding it (NULL = still holds)
- `valid_from` / `valid_to` = when the event was true *in the world* (orthogonal axis)

To query "what did we believe was true in the world at world-time W, as of our knowledge at transaction-time T":
```sql
WHERE b.tx_from <= $1          -- transaction-time T
  AND (b.tx_to IS NULL OR b.tx_to > $1)
  AND b.valid_from <= $2        -- world-time W
  AND (b.valid_to IS NULL OR b.valid_to > $2)
```

---

## Mechanism 2 — MVCC Snapshot (Bounded)

```sql
-- [VERIFY] CockroachDB AS OF SYSTEM TIME syntax
SELECT * FROM belief AS OF SYSTEM TIME $1
WHERE tenant_id = $2;

-- Or as a Python parameter:
-- COCKROACH_URL includes the as-of timestamp as a session variable
-- [VERIFY] exact mechanism for passing AS OF SYSTEM TIME in psycopg
```

Reconstructs the exact committed state at a past instant, including rows since revised — even rows that were later deleted (superseded beliefs still had rows at time T).

**Bounded:** the V3 spike measures the actual retention window. Past that window:
```
ERROR: AS OF SYSTEM TIME: timestamp ... is below the earliest available timestamp
```

---

## Graceful Failure Beyond MVCC Window

When Mechanism 2 is requested beyond the window, fail with a clear error — not silently empty:

```python
def query_mvcc_snapshot(as_of: datetime, tenant_id: UUID, conn) -> list | MVCCWindowError:
    try:
        results = conn.execute(
            f"SELECT * FROM belief AS OF SYSTEM TIME '{as_of.isoformat()}' WHERE tenant_id = %s",
            [tenant_id]
        ).fetchall()
        return results
    except Exception as e:
        if 'below the earliest available timestamp' in str(e):
            raise MVCCWindowExceeded(
                as_of=as_of,
                window_bound=get_mvcc_window_lower_bound(),
                message="MVCC window exceeded. Use bitemporal query for timestamps before this bound."
            )
        raise
```

The API response must make this clear to the caller:
```json
{
  "error": "mvcc_window_exceeded",
  "requested_timestamp": "2024-01-15T14:32:00Z",
  "window_lower_bound": "2024-03-10T08:00:00Z",
  "suggestion": "Use mechanism=bitemporal for timestamps before 2024-03-10T08:00:00Z"
}
```

---

## Phase 6 Exit Gate Test (Design §26.9)

```python
def test_phase_6_exit_gate():
    # Day 1: belief enters as trusted at timestamp T_write
    belief_id = write_and_screen_belief(...)
    T_write = datetime.utcnow()

    # Wait long enough that MVCC window expires (adjust for your V3 measurement)
    # In integration tests, advance time artificially or use a very old T_write from test fixtures

    # Query via bitemporal (Mechanism 1) — must work
    results = bitemporal_query(as_of=T_write, tenant_id=TENANT_ID)
    assert any(b['belief_id'] == belief_id for b in results), \
        "Bitemporal query should find belief at T_write"

    # Query via MVCC (Mechanism 2) for the same timestamp — must fail gracefully
    with pytest.raises(MVCCWindowExceeded) as exc:
        mvcc_query(as_of=T_write - timedelta(days=60), tenant_id=TENANT_ID)

    # The error must explain what happened and suggest the alternative
    assert exc.value.window_lower_bound is not None
    assert 'bitemporal' in str(exc.value.message).lower()
```

---

## Attribution Queries (A10)

Combine both mechanisms with the retrieval log:

**"What did the agent believe at T?"**
```sql
-- Mechanism 1
SELECT b.subject, b.predicate, b.object, b.status, b.author_agent_id,
       p.source_type, p.source_uri
FROM belief b JOIN provenance p ON b.provenance_id = p.provenance_id
WHERE b.tenant_id = $1
  AND b.tx_from <= $2 AND (b.tx_to IS NULL OR b.tx_to > $2)
ORDER BY b.tx_from;
```

**"What was the agent's context when it made decision D?"**
```sql
-- Join retrieval_log with belief at the retrieval timestamp
SELECT b.subject, b.predicate, b.object, b.status
FROM retrieval_log rl
JOIN belief b ON b.belief_id = ANY(rl.returned_belief_ids)
WHERE rl.retrieval_id = $1;
-- Then filter rl.returned_belief_ids against bitemporal view at rl.retrieved_at
```

**"What changed between T1 and T2?"**
```sql
-- Beliefs that appeared after T1
SELECT *, 'new' AS change_type FROM belief
WHERE tenant_id = $1 AND tx_from > $2 AND tx_from <= $3

UNION ALL

-- Beliefs that disappeared before T2
SELECT *, 'removed' AS change_type FROM belief
WHERE tenant_id = $1 AND tx_to > $2 AND tx_to <= $3

ORDER BY tx_from;
```

---

## README Framing (Required)

In the README, state explicitly:

1. **Mechanism 1 (bitemporal)** is the production mechanism for arbitrary historical replay. It answers "what did we believe at T" by filtering on `tx_from`/`tx_to` columns, which are ordinary persisted data.

2. **Mechanism 2 (MVCC)** reconstructs the exact committed state at T, including rows since revised. It is bounded by the garbage-collection retention window (measured at [date] as [N minutes/hours]). Beyond that window, it fails with an explicit error.

3. **Any claim of arbitrary historical replay must be attributed to Mechanism 1.** MVCC cannot support this claim, and a knowledgeable reviewer will know it.

4. **The measured MVCC window is [N].** (Fill in from V3 spike.)
