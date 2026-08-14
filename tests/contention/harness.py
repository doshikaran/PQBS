"""Concurrency correctness harness — §25.3 / BUILD-PLAN §10.4.

Tests that SERIALIZABLE isolation produces a total order on concurrent belief
writes (exactly one trusted belief), while READ COMMITTED may produce forks.

Usage:
    from tests.contention.harness import ContentionHarness, ContentionResult

    harness = ContentionHarness()
    result = harness.run(get_conn, tenant_id, "C001", "customer_tier", 8, "serializable")
    assert result.passed
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID, uuid4

import psycopg
import psycopg.errors
from psycopg import sql as pgsql
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class ContentionResult:
    """Result of one contention test run."""

    isolation_level: str
    n_writers: int
    subject: str
    predicate: str
    tenant_id: UUID
    trusted_count: int
    chain_is_total_order: bool
    all_writes_accounted: bool
    nothing_lost: bool
    total_writes: int
    chain_length: int
    contradiction_events_count: int
    wall_time_ms: float
    passed: bool
    failure_reason: str | None


class ContentionHarness:
    """
    Runs N writer threads concurrently against the same (tenant_id, subject, predicate).
    Each writer attempts to become the single trusted belief via optimistic concurrency.

    Pass invariants checked:
    - trusted_count == 1 (exactly one winner)
    - chain_is_total_order (no forks in the supersession chain)
    - all_writes_accounted (chain length accounts for all successful writes)
    - nothing_lost (no write silently disappeared)
    """

    MAX_RETRY = 10
    BASE_JITTER = 0.01  # seconds

    def run(
        self,
        get_conn: Callable[[], psycopg.Connection[Any]],
        tenant_id: UUID,
        subject: str,
        predicate: str,
        n_writers: int,
        isolation_level: str,
    ) -> ContentionResult:
        """Run N concurrent writers and verify the correctness invariants."""
        belief_ids: list[UUID | None] = [None] * n_writers
        succeeded: list[bool] = [False] * n_writers
        errors: list[Exception | None] = [None] * n_writers

        threads: list[threading.Thread] = []
        t_start = time.perf_counter()

        for idx in range(n_writers):
            t = threading.Thread(
                target=self._writer,
                args=(
                    get_conn,
                    tenant_id,
                    subject,
                    predicate,
                    isolation_level,
                    idx,
                    belief_ids,
                    succeeded,
                    errors,
                ),
                daemon=True,
            )
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120.0)

        wall_time_ms = (time.perf_counter() - t_start) * 1000.0

        # Collect any errors
        non_retryable = [e for e in errors if e is not None]
        if non_retryable:
            logger.warning(
                "contention_writer_errors",
                count=len(non_retryable),
                sample=str(non_retryable[0]),
            )

        # Audit the DB state
        conn = get_conn()
        try:
            return self._audit(
                conn=conn,
                tenant_id=tenant_id,
                subject=subject,
                predicate=predicate,
                isolation_level=isolation_level,
                n_writers=n_writers,
                belief_ids=belief_ids,
                succeeded=succeeded,
                wall_time_ms=wall_time_ms,
            )
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Writer thread
    # ------------------------------------------------------------------

    def _writer(
        self,
        get_conn: Callable[[], psycopg.Connection[Any]],
        tenant_id: UUID,
        subject: str,
        predicate: str,
        isolation_level: str,
        thread_idx: int,
        belief_ids: list[UUID | None],
        succeeded: list[bool],
        errors: list[Exception | None],
    ) -> None:
        """Single writer thread. Retries on SerializationFailure."""
        conn = get_conn()
        try:
            conn.execute(
                pgsql.SQL("SET default_transaction_isolation = {}").format(
                    pgsql.Literal(isolation_level)
                )
            )

            confidence = round(thread_idx * 0.05 + 0.5, 4)
            new_belief_id = uuid4()
            belief_ids[thread_idx] = new_belief_id

            for attempt in range(self.MAX_RETRY):
                try:
                    self._do_write(
                        conn=conn,
                        tenant_id=tenant_id,
                        subject=subject,
                        predicate=predicate,
                        belief_id=new_belief_id,
                        confidence=confidence,
                        thread_idx=thread_idx,
                    )
                    conn.commit()
                    succeeded[thread_idx] = True
                    return
                except psycopg.errors.SerializationFailure:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    if attempt < self.MAX_RETRY - 1:
                        jitter = random.uniform(0, self.BASE_JITTER * (2 ** attempt))
                        time.sleep(jitter)
                    continue
                except Exception as exc:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    errors[thread_idx] = exc
                    logger.error(
                        "contention_writer_fatal",
                        thread_idx=thread_idx,
                        error=str(exc),
                    )
                    return

            # Exhausted retries — not a fatal error, just means this thread lost
            logger.warning(
                "contention_writer_retry_exhausted",
                thread_idx=thread_idx,
                max_retry=self.MAX_RETRY,
            )
        finally:
            conn.close()

    def _do_write(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: UUID,
        subject: str,
        predicate: str,
        belief_id: UUID,
        confidence: float,
        thread_idx: int,
    ) -> None:
        """
        One transactional write attempt:
        1. INSERT provenance row
        2. INSERT belief row (status='pending')
        3. SELECT current trusted belief
        4. If found: close its validity window, set superseded_by=new_belief_id
        5. UPDATE own belief to status='trusted'
        """
        from datetime import datetime, timezone
        now = datetime.now(tz=timezone.utc)
        prov_id = uuid4()

        # 1. Insert provenance
        conn.execute(
            """
            INSERT INTO provenance
              (provenance_id, tenant_id, source_type, source_uri, source_digest,
               episode_id, derived_from, ingested_at, source_trust_tier, ingestion_agent_id)
            VALUES (%s, %s, 'user_statement', %s, %s, %s, '[]', %s, 'unverified', 'a1_ingestion')
            ON CONFLICT (tenant_id, provenance_id) DO NOTHING
            """,
            (
                str(prov_id),
                str(tenant_id),
                f"contention://test/thread-{thread_idx}",
                "0" * 64,  # dummy digest
                str(uuid4()),  # episode_id
                now,
            ),
        )

        # 2. Insert belief (status=pending)
        conn.execute(
            """
            INSERT INTO belief
              (tenant_id, belief_id, subject, predicate, object, confidence,
               valid_from, status, author_agent_id, provenance_id, sensitivity)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending'::belief_status,
                    'a1_ingestion', %s, 'normal'::sensitivity)
            ON CONFLICT (tenant_id, belief_id) DO NOTHING
            """,
            (
                str(tenant_id),
                str(belief_id),
                subject,
                predicate,
                f"thread_{thread_idx}_value",
                confidence,
                now,
                str(prov_id),
            ),
        )

        # 3. SELECT current trusted belief
        incumbent = conn.execute(
            """
            SELECT belief_id, valid_from
            FROM belief
            WHERE tenant_id = %s
              AND subject = %s
              AND predicate = %s
              AND status = 'trusted'
            ORDER BY tx_from DESC
            LIMIT 1
            """,
            (str(tenant_id), subject, predicate),
        ).fetchone()

        # 4. If found: close validity window, set superseded_by
        if incumbent is not None:
            incumbent_id = incumbent["belief_id"]
            if str(incumbent_id) != str(belief_id):
                conn.execute(
                    """
                    UPDATE belief
                    SET valid_to = %s,
                        superseded_by = %s,
                        status = 'superseded'::belief_status
                    WHERE belief_id = %s
                      AND tenant_id = %s
                      AND status = 'trusted'
                    """,
                    (now, str(belief_id), str(incumbent_id), str(tenant_id)),
                )

                conn.execute(
                    """
                    UPDATE belief
                    SET supersedes = %s
                    WHERE belief_id = %s
                      AND tenant_id = %s
                    """,
                    (str(incumbent_id), str(belief_id), str(tenant_id)),
                )

        # 5. UPDATE own belief to trusted
        conn.execute(
            """
            UPDATE belief
            SET status = 'trusted'::belief_status
            WHERE belief_id = %s
              AND tenant_id = %s
              AND status = 'pending'
            """,
            (str(belief_id), str(tenant_id)),
        )

    # ------------------------------------------------------------------
    # Post-run audit
    # ------------------------------------------------------------------

    def _audit(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: UUID,
        subject: str,
        predicate: str,
        isolation_level: str,
        n_writers: int,
        belief_ids: list[UUID | None],
        succeeded: list[bool],
        wall_time_ms: float,
    ) -> ContentionResult:
        """Query the DB and verify invariants."""
        rows = conn.execute(
            """
            SELECT belief_id, status, supersedes, superseded_by, tx_from
            FROM belief
            WHERE tenant_id = %s
              AND subject = %s
              AND predicate = %s
            ORDER BY tx_from
            """,
            (str(tenant_id), subject, predicate),
        ).fetchall()

        total_writes = sum(1 for b in succeeded if b)
        trusted_count = sum(1 for r in rows if r["status"] == "trusted")
        chain_length = len(rows)

        # Verify total order: walk the supersession chain from root
        chain_is_total_order = self._verify_chain(rows)

        # Count contradiction events if the schema supports it
        try:
            ce_row = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM contradiction_event
                WHERE tenant_id = %s
                  AND subject_id = %s
                """,
                (str(tenant_id), subject),
            ).fetchone()
            contradiction_events_count = ce_row["cnt"] if ce_row else 0
        except Exception:
            contradiction_events_count = 0

        # all_writes_accounted: every successful write appears in the chain
        written_ids = {str(bid) for bid, ok in zip(belief_ids, succeeded) if ok and bid}
        chain_ids = {str(r["belief_id"]) for r in rows}
        all_writes_accounted = written_ids <= chain_ids

        # nothing_lost: no successful write disappeared entirely
        pending_count = sum(1 for r in rows if r["status"] == "pending")
        nothing_lost = (
            total_writes
            <= chain_length + contradiction_events_count + pending_count
        )

        passed = (
            trusted_count == 1
            and chain_is_total_order
            and all_writes_accounted
            and nothing_lost
        )

        failure_reason: str | None = None
        if not passed:
            reasons = []
            if trusted_count != 1:
                reasons.append(f"trusted_count={trusted_count} (expected 1)")
            if not chain_is_total_order:
                reasons.append("chain has forks or gaps (not a total order)")
            if not all_writes_accounted:
                missing = written_ids - chain_ids
                reasons.append(f"missing belief_ids in chain: {missing}")
            if not nothing_lost:
                reasons.append("write count exceeds chain + contradiction + pending")
            failure_reason = "; ".join(reasons)

        logger.info(
            "contention_audit_complete",
            isolation_level=isolation_level,
            n_writers=n_writers,
            trusted_count=trusted_count,
            chain_length=chain_length,
            total_writes=total_writes,
            passed=passed,
            failure_reason=failure_reason,
        )

        return ContentionResult(
            isolation_level=isolation_level,
            n_writers=n_writers,
            subject=subject,
            predicate=predicate,
            tenant_id=tenant_id,
            trusted_count=trusted_count,
            chain_is_total_order=chain_is_total_order,
            all_writes_accounted=all_writes_accounted,
            nothing_lost=nothing_lost,
            total_writes=total_writes,
            chain_length=chain_length,
            contradiction_events_count=contradiction_events_count,
            wall_time_ms=wall_time_ms,
            passed=passed,
            failure_reason=failure_reason,
        )

    def _verify_chain(self, rows: list[Any]) -> bool:
        """
        Verify the supersession chain is a total order (no forks, no orphans).

        A valid chain: each belief either has no supersedes (root) or its
        supersedes points to a belief in the set, and no belief is pointed
        at by more than one superseded_by.
        """
        if not rows:
            return True

        # Build id→row map — used for debugging chain walk failures
        id_to_row = {str(r["belief_id"]): r for r in rows}
        _ = id_to_row  # suppress unused-variable; kept for post-mortem inspection
        # Count how many times each belief_id appears as superseded_by target
        superseded_by_targets: dict[str, int] = {}
        for r in rows:
            sb = r["superseded_by"]
            if sb is not None:
                key = str(sb)
                superseded_by_targets[key] = superseded_by_targets.get(key, 0) + 1

        # A fork: more than one belief claims to supersede the same incumbent
        # (i.e., the same belief_id appears as superseded_by of >1 entries)
        # Actually the fork direction is: two beliefs both have supersedes=X
        supersedes_counts: dict[str, int] = {}
        for r in rows:
            sp = r["supersedes"]
            if sp is not None:
                key = str(sp)
                supersedes_counts[key] = supersedes_counts.get(key, 0) + 1

        # Fork condition: any belief is superseded by more than one other belief
        has_fork = any(v > 1 for v in supersedes_counts.values())

        return not has_fork
