# Skill: Testing Patterns for PQBS

Use this skill when writing pytest tests, integration tests, negative tests, or failure-mode tests for PQBS components.

---

## Test Directory Structure

```
tests/
├── unit/                    # Fast; no DB; mock boundaries
│   ├── test_contracts.py    # Pydantic model validation
│   ├── test_signals.py      # Signal logic with mocked data
│   ├── test_resolution.py   # Resolution precedence logic
│   └── test_canonicalize.py # Canonicalization rules
├── integration/             # Real DB; isolated test tenant
│   ├── test_schema.py       # Migration apply/rollback
│   ├── test_constraints.py  # Negative tests (role isolation, lifecycle)
│   ├── test_write_path.py   # End-to-end write with retry
│   ├── test_screening.py    # CDC → screening → verdict
│   ├── test_cascade.py      # Cascade completeness, cycle safety
│   ├── test_recall.py       # Recall filtering, retrieval_log
│   ├── test_audit.py        # Bitemporal + MVCC queries
│   └── test_failure_modes.py # All 13 failure-mode rows
├── contention/              # Concurrency harness (E5 owns)
│   ├── compare.py           # Serializable vs READ COMMITTED comparison
│   └── harness.py           # N-concurrent-writer test
└── evaluation/              # Red-team harness (E5 owns)
    └── run.py               # Full evaluation pipeline
```

---

## Integration Test Setup

Use a dedicated test tenant to avoid polluting the demo data:

```python
# tests/conftest.py
import pytest
import os
import psycopg
from uuid import uuid4

TEST_TENANT_ID = uuid4()   # new UUID per test session

@pytest.fixture(scope='session')
def db_conn():
    conn = psycopg.connect(os.environ['COCKROACH_URL'])
    yield conn
    conn.close()

@pytest.fixture(autouse=True)
def clean_test_tenant(db_conn):
    # Clean up test tenant data before each test
    yield
    db_conn.execute(
        "DELETE FROM belief WHERE tenant_id = %s",
        [TEST_TENANT_ID]
    )
    db_conn.commit()
```

---

## Negative Tests (Required for Phase 2 Exit Gate)

These are the most important integration tests in Phase 2. They prove the authority matrix is enforced by the database.

```python
# tests/integration/test_constraints.py

def test_role_consumer_cannot_read_quarantined(consumer_conn):
    """role_consumer must not see quarantined beliefs — structural enforcement"""
    # Assume a quarantined belief exists in the test tenant
    results = consumer_conn.execute(
        "SELECT * FROM belief WHERE status = 'quarantined' AND tenant_id = %s",
        [TEST_TENANT_ID]
    ).fetchall()
    assert len(results) == 0, "role_consumer accessed quarantined content"

def test_role_consumer_cannot_bypass_via_view(consumer_conn, quarantined_belief_id):
    """Even if the attacker knows the belief_id, role_consumer cannot retrieve it"""
    results = consumer_conn.execute(
        "SELECT * FROM trusted_current_beliefs WHERE belief_id = %s",
        [quarantined_belief_id]
    ).fetchall()
    assert len(results) == 0

def test_role_producer_cannot_insert_trusted(producer_conn):
    """role_producer cannot write status=trusted — DB constraint must block it"""
    with pytest.raises(Exception):   # psycopg raises on constraint violation
        producer_conn.execute(
            """INSERT INTO belief (tenant_id, belief_id, subject, predicate, object,
                                   object_normalized, confidence, valid_from, tx_from,
                                   status, author_agent_id, provenance_id)
               VALUES (%s, gen_random_uuid(), 'test', 'test', 'test', 'test',
                       0.9, NOW(), NOW(), 'trusted', 'attacker', gen_random_uuid())""",
            [TEST_TENANT_ID]
        )
        producer_conn.commit()

def test_every_belief_enters_pending(write_belief_fn):
    """No write path produces status != pending"""
    belief_id = write_belief_fn(subject="S", predicate="P", object="O")
    status = get_status(belief_id)
    assert status == 'pending', f"Expected pending, got {status}"
```

---

## Fail-Closed Tests

```python
def test_fail_closed_worker_down(db_conn):
    """When screening worker is down, zero beliefs become retrievable"""
    stop_screening_worker()
    try:
        belief_ids = [write_belief_raw(db_conn) for _ in range(10)]
        time.sleep(2)   # give CDC time to emit

        # All must be pending
        for bid in belief_ids:
            assert get_status(db_conn, bid) == 'pending'

        # role_consumer retrieves zero
        with connect_as('role_consumer') as c:
            results = c.execute(
                "SELECT * FROM trusted_current_beliefs WHERE tenant_id = %s",
                [TEST_TENANT_ID]
            ).fetchall()
            assert len(results) == 0
    finally:
        start_screening_worker()
```

---

## Cascade Tests

```python
def test_cascade_completeness(db_conn):
    """100% of descendants must be re-screened when parent is quarantined"""
    a = write_and_trust(db_conn, subject="A")
    b = write_and_trust(db_conn, subject="B", derived_from=[a])
    c = write_and_trust(db_conn, subject="C", derived_from=[b])

    # Quarantine A
    quarantine_belief(db_conn, a, reason='imperative_content')
    run_cascade(db_conn, a)

    # B and C must be pending
    assert get_status(db_conn, b) == 'pending'
    assert get_status(db_conn, c) == 'pending'

    # Cascade depth recorded
    assert get_cascade_depth(db_conn, a) == 2

def test_cascade_cycle_safe(db_conn):
    """Cycle in derivation graph must not cause infinite loop"""
    x = write_and_trust(db_conn, subject="X")
    y = write_and_trust(db_conn, subject="Y", derived_from=[x])
    set_derived_from(db_conn, x, [y])   # create cycle

    quarantine_belief(db_conn, x, reason='manual')

    with pytest.raises(TimeoutError):
        pass
    else:
        with timeout(seconds=5):
            run_cascade(db_conn, x)   # must complete within 5s

def test_cascade_idempotent(db_conn):
    """Running cascade twice produces same result"""
    a = write_and_trust(db_conn, subject="A")
    b = write_and_trust(db_conn, subject="B", derived_from=[a])
    quarantine_belief(db_conn, a, reason='manual')

    run_cascade(db_conn, a)
    run_cascade(db_conn, a)   # duplicate

    # No double re-screening; still pending once
    assert get_status(db_conn, b) == 'pending'
    assert count_cascade_log_entries(db_conn, a) == 1
```

---

## Failure-Mode Tests (13 Rows)

Each row in design §24 becomes an integration test. The most important ones:

```python
# 1. Screening worker down → covered by test_fail_closed_worker_down above

# 2. Retry exhaustion → no isolation downgrade
def test_retry_exhaustion_no_downgrade(db_conn):
    with force_max_retries():
        with pytest.raises(ContentionError):
            write_belief_serializable(db_conn, ...)
    # Verify the error is a ContentionError, not a success at a lower isolation level

# 3. Cascade cycle → halt and flag
# → test_cascade_cycle_safe above

# 4. Audit sink unavailable → write blocked
def test_audit_sink_unavailable_blocks_write(db_conn):
    with mock_s3_unavailable():
        with pytest.raises(WriteRejectedAuditUnavailable):
            write_belief_raw(db_conn, ...)

# 5. Tenant isolation — adversarial
def test_tenant_isolation_adversarial(db_conn):
    """Tenant B cannot see Tenant A's beliefs, even with direct SQL"""
    tenant_a = uuid4()
    tenant_b = uuid4()

    a_belief = write_and_trust_for_tenant(db_conn, tenant_a)

    # Connect as Tenant B's consumer
    with connect_as_tenant('role_consumer', tenant_b) as c:
        # Even with direct query on belief_id, must return nothing
        results = c.execute(
            "SELECT * FROM trusted_current_beliefs WHERE belief_id = %s",
            [a_belief]
        ).fetchall()
        assert len(results) == 0, "Cross-tenant retrieval succeeded — structural failure"
```

---

## Test Naming Convention

```
test_<what>_<condition>_<expected_outcome>

Examples:
test_role_consumer_cannot_read_quarantined
test_retry_wrapper_rerads_state_on_conflict
test_cascade_cycle_halts_without_hanging
test_screening_gate_idempotent_on_duplicate_event
test_mvcc_query_fails_gracefully_beyond_window
```

---

## Required Test Evidence Before Phase Gates

| Phase | Required evidence |
|---|---|
| P2 | Negative tests: consumer can't read quarantined, producer can't write trusted |
| P3 | Contention harness passes with 8+ writers; latency measured |
| P4 | Fail-closed test passes; screening lag measured |
| P5 | Cascade completeness 100%; cycle test; WORM delete fails |
| P6 | Role-bypass test; bitemporal beyond MVCC window; recall latency measured |
| P7 | All 13 failure-mode tests pass |
| P8 | Evaluation metrics committed to eval/results/ |
