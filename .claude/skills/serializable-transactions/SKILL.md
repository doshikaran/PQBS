# Skill: Serializable Transactions and Retry Semantics

Use this skill when implementing the retry wrapper, understanding contention behavior, or writing tests that exercise serializable isolation.

---

## What a Serializable Retry Is

Under serializable isolation, CockroachDB detects when a transaction's read set has been made stale by a concurrent committed write. The transaction is aborted with SQLSTATE `40001` (serialization failure). The application must retry the entire transaction, including re-reading all rows.

**This is not an error to suppress.** It is the database telling you: "the world changed while you were looking; look again." The retry's re-reads are the mechanism by which the system ensures consistency.

---

## The Retry Wrapper — Required Behavior

### What MUST happen on retry

The entire transaction body must re-execute, including all reads. Any object read in the first attempt must be re-queried in the retry. There is no exception to this.

**Why:** if you cache reads between attempts, the retry evaluates contradiction logic against the stale state from attempt 1. The serializable anomaly it was designed to catch goes uncaught. This is the most common implementation error in retry wrappers, and it silently reintroduces exactly the problem the design exists to prevent.

```python
# WRONG — stale read reused
incumbent = conn.execute(query_incumbent, args).first()   # read once, before retry loop
for attempt in range(max):
    with conn.begin():
        resolve(incumbent, challenger)    # stale: incumbent from before concurrent commit

# CORRECT — read inside retry loop
for attempt in range(max):
    with conn.begin() as txn:
        incumbent = txn.execute(query_incumbent, args).first()  # re-read each time
        resolve(incumbent, challenger)
```

### What to catch

Only catch `psycopg.errors.SerializationFailure` (SQLSTATE 40001) or the equivalent ORM exception. All other errors propagate immediately without retry.

```python
from psycopg.errors import SerializationFailure

for attempt in range(max_attempts):
    try:
        with conn.begin():
            result = txn_fn(conn)
            return result, attempt
    except SerializationFailure:
        if attempt == max_attempts - 1:
            raise ContentionError(f"serializable retry exhausted after {max_attempts} attempts")
        sleep(backoff(attempt))
    # All other exceptions propagate immediately
```

### Backoff

Bounded exponential backoff with jitter:
```python
def backoff(attempt: int, base_ms: float = 50.0, max_ms: float = 2000.0) -> float:
    raw = base_ms * (2 ** attempt)
    jitter = random.uniform(-0.25 * raw, 0.25 * raw)
    return min(raw + jitter, max_ms) / 1000.0   # return seconds
```

### Retry counting

Surface `retry_count` for `contradiction_event` rows. The count is the number of serializable failures before the final successful commit (not counting the successful attempt).

### On exhaustion

Raise a specific `ContentionError` (or equivalent named exception). **Never fall back to a weaker isolation level.** Downgrading to READ COMMITTED on exhaustion defeats the entire design — READ COMMITTED is precisely what the system is designed to avoid under concurrent writes.

```python
# NEVER do this:
except ContentionError:
    with conn.execution_options(isolation_level='READ COMMITTED').begin():
        return txn_fn(conn)   # wrong: removes the guarantee
```

---

## Contention Harness Integration

E5 owns `tests/contention/`. The harness drives N concurrent writers (default: 16) asserting conflicting values for the same `(tenant_id, subject, predicate)`.

After quiescence, the harness asserts:
1. Exactly one belief has `status = 'trusted'` for the contested key (single-valued predicate)
2. The supersession chain is a total order — no forks, no branches
3. Every write appears exactly once in either the chain or `contradiction_event`
4. Nothing was lost

Your retry wrapper must produce a `retry_count > 0` on most runs under 16 concurrent writers. The harness also verifies the READ COMMITTED failure for comparison.

**Run before reporting Phase 3 complete:**
```bash
python -m tests.contention.compare --isolation serializable --writers 16
```

Expected: retry rate > 30% (this is a floor — the test exists to force retries).

---

## Embedding Before the Transaction (Critical Ordering)

Embedding (A12 model call) must happen **before** opening the serializable transaction. Holding an open serializable transaction across a network call to an embedding service has two failure modes:

1. **Contention:** the transaction holds read locks while waiting for the model; other writers block or retry unnecessarily.
2. **Timeout:** if the embedding call is slow (or the service is degraded), the transaction times out with a partial-write anomaly.

```python
# CORRECT order
embedding = compute_embedding(text)          # outside transaction
with conn.begin():                           # transaction opens AFTER embedding
    incumbent = query_incumbent(conn, ...)
    resolve_and_insert(conn, embedding, ...)
```

---

## What Constitutes a Retryable Transaction

The write-path transaction body includes:
1. Query `predicate_policy` for cardinality and resolution strategy
2. Query currently-trusted beliefs for `(tenant_id, subject, predicate)` (if single-valued)
3. Apply resolution logic (A7)
4. Close incumbent's `tx_to` and `valid_to` if superseded
5. Insert new belief as `pending`
6. Insert `contradiction_event` row

All of steps 1–6 must be inside the retry loop. Step 2 is the critical re-read — it is what sees the concurrent commit.

---

## What Is NOT Retryable

- Embedding computation (A12) — happens before the transaction
- Canonicalization (A11) — happens before the transaction
- Provenance record insertion can be inside the transaction (it's fast)
- Audit record emission — happens after successful commit, not inside the transaction

---

## Tests Required

| Test | Assertion |
|---|---|
| `test_retry_rerads_on_conflict` | Under concurrent writes, retry count > 0 observed |
| `test_no_stale_reads` | Cached read detected and rejected (assert correct final state) |
| `test_no_isolation_downgrade` | ContentionError raised on exhaustion; no fallback to READ COMMITTED |
| `test_retry_count_recorded` | `contradiction_event.retry_count` matches observed retries |
| `test_contention_harness_serializable` | 16 writers → total-ordered chain, nothing lost |
| `test_contention_harness_read_committed` | Same harness under READ COMMITTED → demonstrates fork/loss |
| `test_contention_negative_retry_rate` | Normal load → retry rate < 5% |
| `test_contention_positive_retry_rate` | Contention test → retry rate > 30% |

---

## SQLSTATE Reference

| Code | Meaning |
|---|---|
| `40001` | Serialization failure — retry entire transaction |
| `40P01` | Deadlock — also retryable in some configurations |
| `57P04` | Database not available — do NOT retry, propagate |

---

## Relationship to V5 Spike

The Phase 0 V5 spike verified that serializable retries fire reliably (≥90% across 20 runs) under a scripted timing pattern. The production retry wrapper must produce the same reliability under the Phase 3 contention harness. If the harness cannot force retries, either the isolation level is not serializable or the transaction body is not producing conflicts — investigate immediately.
