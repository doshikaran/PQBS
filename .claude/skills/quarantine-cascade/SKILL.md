# Skill: Quarantine Cascade and Lifecycle

Use this skill when implementing cascade traversal (A6), managing quarantine state transitions, building cycle-safe graph traversal, or tracking cascade depth.

---

## Why Cascade Exists

A quarantine that stops at the root while leaving derivatives trusted has contained nothing. The design captures derivation through `provenance.derived_from`. When a belief is quarantined, everything transitively derived from it must be re-screened — because poison propagates through inference.

From design §9.2: "`derived_from` is load-bearing and easy to overlook: if a parent is later quarantined, everything inferred from it must be re-screened."

**E3 also owns A18 and A19** (Phase 6.5), which extend containment beyond the belief graph into the substrate layer. A18 verifies that the database posture (role grants, constraints, views, indexes) has not drifted. A19 monitors the control plane for admin actions. These are co-resident in E3's domain because they are all containment mechanisms — they differ only in what they contain (belief propagation vs. schema/control-plane drift). Coordinate with E3 lead when scheduling work across A6/A14 (Phase 5) and A18/A19 (Phase 6.5).

---

## Two Non-Negotiable Properties

**Idempotent:** the same quarantine event processed twice must produce the same result. Duplicate CDC events or re-triggered cascade workers must not double-re-screen or create duplicate quarantine records.

**Cycle-safe:** derivation graphs are not reliably acyclic. A belief derived from B, B derived from A = cycle. An unguarded traversal hangs. Maintain a visited set; on cycle detection, halt (don't error) and continue with the rest of the graph.

---

## Cascade Implementation

```python
from collections import deque
from uuid import UUID
from typing import Set, Tuple

def cascade_quarantine(
    quarantined_belief_id: UUID,
    tenant_id: UUID,
    conn,
    visited: Set[UUID] | None = None
) -> Tuple[Set[UUID], int]:
    """
    Returns (descendants_to_rescreen, max_depth).
    Idempotent: call with same quarantined_belief_id → same result.
    Cycle-safe: visited set prevents infinite loops.
    """
    if visited is None:
        visited = set()

    queue = deque([(quarantined_belief_id, 0)])
    descendants = set()
    max_depth = 0

    while queue:
        current_id, depth = queue.popleft()

        if current_id in visited:
            # Cycle detected — skip this node, flag for review
            log.warning("cascade_cycle_detected",
                        belief_id=str(current_id),
                        depth=depth)
            continue

        visited.add(current_id)
        max_depth = max(max_depth, depth)

        # Find beliefs where derived_from contains current_id
        children = conn.execute(
            """SELECT b.belief_id FROM belief b
               JOIN provenance p ON b.provenance_id = p.provenance_id
               WHERE b.tenant_id = %s AND %s = ANY(p.derived_from)
               AND b.status NOT IN ('rejected', 'superseded')""",
            [tenant_id, current_id]
        ).fetchall()

        for child in children:
            child_id = child['belief_id']
            if child_id not in visited:
                descendants.add(child_id)
                queue.append((child_id, depth + 1))

    return descendants, max_depth


def process_cascade(quarantine_record: QuarantineRecord, conn) -> None:
    # Idempotency check: has this quarantine event already been cascaded?
    already_processed = conn.execute(
        """SELECT 1 FROM cascade_log
           WHERE belief_id = %s AND quarantined_at = %s""",
        [quarantine_record.belief_id, quarantine_record.quarantined_at]
    ).first()

    if already_processed:
        return

    descendants, depth = cascade_quarantine(
        quarantine_record.belief_id,
        quarantine_record.tenant_id,
        conn
    )

    # Request re-screening for all descendants
    for belief_id in descendants:
        conn.execute(
            """UPDATE belief SET status = 'pending', screened_at = NULL
               WHERE belief_id = %s AND tenant_id = %s
               AND status = 'trusted'""",
            [belief_id, quarantine_record.tenant_id]
        )
        # Emit re-screen event (the CDC will pick it up, or insert directly into screening queue)

    # Record cascade completion (for idempotency and metrics)
    conn.execute(
        """INSERT INTO cascade_log (belief_id, quarantined_at, descendant_count, max_depth, cascaded_at)
           VALUES (%s, %s, %s, %s, NOW())""",
        [quarantine_record.belief_id, quarantine_record.quarantined_at, len(descendants), depth]
    )

    metrics.record('cascade_depth', depth)
    metrics.record('cascade_descendants', len(descendants))
```

---

## Testing Cascade

### Correctness test

```python
def test_cascade_completeness():
    # Setup: belief A → B → C → D (chain of derivations)
    a, b, c, d = create_belief_chain(depth=4)

    # Quarantine A
    quarantine(a)
    run_cascade(a)

    # Assert all descendants are pending (re-screening requested)
    for belief_id in [b, c, d]:
        assert get_status(belief_id) == 'pending'

    # Assert cascade depth recorded correctly
    assert get_cascade_depth(a) == 3
```

### Cycle safety test

```python
def test_cascade_cycle_safe():
    # Create a cycle: X derives from Y, Y derives from X
    x = create_belief()
    y = create_belief(derived_from=[x])
    set_derived_from(x, [y])   # create the cycle

    # Quarantine X — traversal must halt, not hang
    quarantine(x)
    with timeout(seconds=5):   # must complete within 5 seconds
        run_cascade(x)

    # Cycle warning must be logged
    assert 'cascade_cycle_detected' in captured_logs
```

### Idempotency test

```python
def test_cascade_idempotent():
    a, b = create_belief_chain(depth=2)
    quarantine(a)

    # Process the cascade event twice
    run_cascade(a)
    run_cascade(a)   # duplicate

    # Descendant should be pending exactly once, not double-reset
    assert get_status(b) == 'pending'
    # No duplicate cascade_log entries
    assert count_cascade_log_entries(a) == 1
```

---

## Quarantine Lifecycle

```
  A4 screening → QUARANTINED
                     │
                     ├── A6 cascade fires (async)
                     │       └── descendants → pending → re-screen
                     │
                     └── A14 review queue
                              │
                      ┌───────┴───────┐
                      │               │
                  released          rejected
                (requires         (terminal;
                reviewer_id)      never deleted)
```

### Release Requires Human Reviewer

```python
def release_belief(quarantine_id: UUID, reviewer_id: str, notes: str, conn) -> None:
    if not reviewer_id or not reviewer_id.strip():
        raise ValueError("reviewer_id is required for release — no autonomous release")

    conn.execute(
        """UPDATE quarantine
           SET disposition = 'released', reviewed_by = %s, review_notes = %s, reviewed_at = NOW()
           WHERE quarantine_id = %s""",
        [reviewer_id, notes, quarantine_id]
    )

    conn.execute(
        "UPDATE belief SET status = 'trusted', screened_at = NOW() WHERE belief_id = (SELECT belief_id FROM quarantine WHERE quarantine_id = %s)",
        [quarantine_id]
    )

    emit_audit_record('released', reviewer_id, ...)
```

**No timeout-to-release.** If the review queue is unattended, items stay held indefinitely. Held is the safe state (design §24).

---

## Cascade Depth as Observability

Cascade depth is a metric, not just a debug value. Surface it:

```python
# From A17 telemetry
metrics:
  cascade_depth_p50: 2
  cascade_depth_p99: 15
  cascade_depth_max: 47    # interesting — a deep derivation chain worth investigating
```

A quarantine with depth 40 is a very different incident from depth 0. Include it in the dashboard and the demo.
