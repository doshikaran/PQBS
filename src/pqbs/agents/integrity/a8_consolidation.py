"""A8 — Consolidation Agent.

Periodic hygiene: merges exact-duplicate trusted beliefs, compacts long
supersession chains, and verifies working_memory TTL effectiveness.

Security invariants enforced:
  - **T9 boundary**: A8 NEVER merges across a quarantine boundary. Before
    merging any duplicate group, all member belief_ids are checked against
    the quarantine table. If any member has ever been quarantined, the entire
    group is skipped.
  - Conservative by default: when uncertain, do nothing. An over-eager
    consolidator is itself a memory-corruption vector.
  - Authority: uses role_semantics — may update supersession fields and
    status='superseded'. May NOT set status='trusted'. May NOT create beliefs.

Design decisions:
  - Exact-duplicate compaction merges beliefs with identical
    (subject, predicate, object_normalized) in status='trusted'.
    The winner is the belief with the highest confidence; ties broken by
    most-recent valid_from. Losers are superseded (not deleted — audit trail
    must be preserved).
  - Chain flagging reports supersession chains of depth > MAX_CHAIN_DEPTH
    but does NOT automatically collapse them (human review required for
    deeper structural changes).
  - Working memory TTL is enforced by CockroachDB's built-in row TTL job.
    A8 verifies the job is having effect by counting overdue rows; it does
    not re-implement TTL itself.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import psycopg
import structlog

from pqbs.contracts.exceptions import ConsolidationError

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Supersession chains longer than this are flagged for review.
_DEFAULT_MAX_CHAIN_DEPTH: int = int(os.environ.get("PQBS_MAX_CHAIN_DEPTH", "10"))

# Working memory rows older than this many seconds despite having a past TTL
# are counted as TTL failures.
_DEFAULT_TTL_OVERDUE_THRESHOLD_SECS: int = int(
    os.environ.get("PQBS_TTL_OVERDUE_SECS", "3600")
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class DuplicateGroup:
    """A group of trusted beliefs with identical (subject, predicate, object_normalized)."""

    subject: str
    predicate: str
    object_normalized: str
    belief_ids: list[str]   # ordered: winner first (highest confidence)
    winner_id: str
    superseded_ids: list[str]
    skipped_reason: str | None = None  # set if group was not merged


@dataclass
class ChainFlag:
    """A supersession chain flagged as too long."""

    root_belief_id: str
    estimated_depth: int


@dataclass
class ConsolidationRun:
    """Result of a full consolidation pass."""

    tenant_id: str
    started_at: datetime
    completed_at: datetime

    # Exact-duplicate compaction
    duplicate_groups_found: int = 0
    duplicate_groups_merged: int = 0
    duplicate_groups_skipped: int = 0   # skipped due to quarantine boundary
    beliefs_superseded: int = 0

    # Chain flagging (no auto-action; human review required)
    chains_flagged: int = 0
    chain_flags: list[ChainFlag] = field(default_factory=list)

    # Working memory TTL verification
    working_memory_overdue_count: int = 0

    @property
    def elapsed_ms(self) -> int:
        return int((self.completed_at - self.started_at).total_seconds() * 1000)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class ConsolidationAgent:
    """A8 Consolidation Agent — run periodically (e.g., hourly via scheduler)."""

    def run(
        self,
        tenant_id: UUID,
        conn: psycopg.Connection[Any],
        *,
        max_chain_depth: int = _DEFAULT_MAX_CHAIN_DEPTH,
        ttl_overdue_threshold_secs: int = _DEFAULT_TTL_OVERDUE_THRESHOLD_SECS,
        dry_run: bool = False,
    ) -> ConsolidationRun:
        """Run a full consolidation pass for tenant_id.

        Args:
            tenant_id: Tenant to consolidate.
            conn: Open psycopg3 connection. Must be in transactional (autocommit=False) mode.
            max_chain_depth: Flag supersession chains longer than this.
            ttl_overdue_threshold_secs: Log a warning if working_memory rows are
                overdue by more than this many seconds.
            dry_run: If True, detect but do not apply any changes. Useful for auditing.

        Returns:
            ConsolidationRun with a summary of what was done.

        Raises:
            ConsolidationError: on unrecoverable failures.
        """
        started_at = datetime.now(tz=timezone.utc)
        run = ConsolidationRun(
            tenant_id=str(tenant_id),
            started_at=started_at,
            completed_at=started_at,
        )

        logger.info(
            "a8_consolidation_started",
            tenant_id=str(tenant_id),
            dry_run=dry_run,
        )

        try:
            self._compact_duplicates(tenant_id, conn, run, dry_run=dry_run)
            self._flag_long_chains(tenant_id, conn, run, max_chain_depth=max_chain_depth)
            self._verify_ttl(tenant_id, conn, run, overdue_secs=ttl_overdue_threshold_secs)
        except ConsolidationError:
            raise
        except Exception as exc:
            raise ConsolidationError(
                f"Consolidation failed for tenant {tenant_id}: {exc}"
            ) from exc

        run.completed_at = datetime.now(tz=timezone.utc)

        logger.info(
            "a8_consolidation_complete",
            tenant_id=str(tenant_id),
            elapsed_ms=run.elapsed_ms,
            duplicate_groups_found=run.duplicate_groups_found,
            duplicate_groups_merged=run.duplicate_groups_merged,
            duplicate_groups_skipped=run.duplicate_groups_skipped,
            beliefs_superseded=run.beliefs_superseded,
            chains_flagged=run.chains_flagged,
            working_memory_overdue_count=run.working_memory_overdue_count,
            dry_run=dry_run,
        )
        return run

    # ------------------------------------------------------------------
    # Step 1 — Exact-duplicate compaction
    # ------------------------------------------------------------------

    def _compact_duplicates(
        self,
        tenant_id: UUID,
        conn: psycopg.Connection[Any],
        run: ConsolidationRun,
        dry_run: bool,
    ) -> None:
        """Find and merge trusted beliefs with identical (subject, predicate, object_normalized)."""
        rows = conn.execute(
            """
            SELECT
                subject,
                predicate,
                object_normalized,
                array_agg(belief_id::text ORDER BY confidence DESC, valid_from DESC) AS belief_ids
            FROM belief
            WHERE tenant_id = %s
              AND status = 'trusted'
              AND object_normalized IS NOT NULL
              AND object_normalized != ''
            GROUP BY subject, predicate, object_normalized
            HAVING count(*) > 1
            """,
            (str(tenant_id),),
        ).fetchall()

        run.duplicate_groups_found = len(rows)
        if not rows:
            logger.debug("a8_no_duplicates_found", tenant_id=str(tenant_id))
            return

        for row in rows:
            belief_ids: list[str] = list(row["belief_ids"])
            winner_id = belief_ids[0]
            loser_ids = belief_ids[1:]

            group = DuplicateGroup(
                subject=row["subject"],
                predicate=row["predicate"],
                object_normalized=row["object_normalized"],
                belief_ids=belief_ids,
                winner_id=winner_id,
                superseded_ids=loser_ids,
            )

            # T9 security check: skip if any member was ever quarantined
            if self._any_quarantined(belief_ids, tenant_id, conn):
                group.skipped_reason = "quarantine boundary: one or more members quarantined"
                run.duplicate_groups_skipped += 1
                logger.warning(
                    "a8_duplicate_group_skipped_quarantine",
                    tenant_id=str(tenant_id),
                    subject=row["subject"],
                    predicate=row["predicate"],
                    belief_ids=belief_ids,
                )
                continue

            if not dry_run:
                self._merge_group(group, tenant_id, conn)
                conn.commit()

            run.duplicate_groups_merged += 1
            run.beliefs_superseded += len(loser_ids)

            logger.info(
                "a8_duplicate_group_merged",
                tenant_id=str(tenant_id),
                subject=row["subject"],
                predicate=row["predicate"],
                winner_id=winner_id,
                loser_count=len(loser_ids),
                dry_run=dry_run,
            )

    def _any_quarantined(
        self,
        belief_ids: list[str],
        tenant_id: UUID,
        conn: psycopg.Connection[Any],
    ) -> bool:
        """Return True if any belief in the list has ever been quarantined."""
        row = conn.execute(
            """
            SELECT 1 FROM quarantine
            WHERE tenant_id = %s
              AND belief_id = ANY(%s::uuid[])
            LIMIT 1
            """,
            (str(tenant_id), belief_ids),
        ).fetchone()
        return row is not None

    def _merge_group(
        self,
        group: DuplicateGroup,
        tenant_id: UUID,
        conn: psycopg.Connection[Any],
    ) -> None:
        """Supersede losers in a duplicate group. Winner keeps status='trusted'."""
        now = datetime.now(tz=timezone.utc)

        for loser_id in group.superseded_ids:
            conn.execute(
                """
                UPDATE belief
                SET
                    status = 'superseded'::belief_status,
                    valid_to = %s,
                    superseded_by = %s::uuid
                WHERE belief_id = %s::uuid
                  AND tenant_id = %s
                  AND status = 'trusted'
                """,
                (now, group.winner_id, loser_id, str(tenant_id)),
            )
            # Record supersession pointer on winner
            conn.execute(
                """
                UPDATE belief
                SET supersedes = %s::uuid
                WHERE belief_id = %s::uuid
                  AND tenant_id = %s
                """,
                (loser_id, group.winner_id, str(tenant_id)),
            )

    # ------------------------------------------------------------------
    # Step 2 — Long chain flagging
    # ------------------------------------------------------------------

    def _flag_long_chains(
        self,
        tenant_id: UUID,
        conn: psycopg.Connection[Any],
        run: ConsolidationRun,
        max_chain_depth: int,
    ) -> None:
        """Flag supersession chains longer than max_chain_depth.

        Uses a heuristic: beliefs that have been superseded by many others
        (high in-degree on the superseded_by graph) indicate a long chain.
        This does not perform a full graph traversal (cycle-safe by design).
        """
        rows = conn.execute(
            """
            SELECT
                superseded_by::text AS root_id,
                count(*) AS predecessors
            FROM belief
            WHERE tenant_id = %s
              AND status = 'superseded'
              AND superseded_by IS NOT NULL
            GROUP BY superseded_by
            HAVING count(*) >= %s
            ORDER BY predecessors DESC
            LIMIT 50
            """,
            (str(tenant_id), max_chain_depth),
        ).fetchall()

        for row in rows:
            flag = ChainFlag(
                root_belief_id=str(row["root_id"]),
                estimated_depth=int(row["predecessors"]),
            )
            run.chain_flags.append(flag)
            logger.warning(
                "a8_long_chain_detected",
                tenant_id=str(tenant_id),
                root_belief_id=flag.root_belief_id,
                estimated_depth=flag.estimated_depth,
                max_chain_depth=max_chain_depth,
            )

        run.chains_flagged = len(rows)

    # ------------------------------------------------------------------
    # Step 3 — Working memory TTL verification
    # ------------------------------------------------------------------

    def _verify_ttl(
        self,
        tenant_id: UUID,
        conn: psycopg.Connection[Any],
        run: ConsolidationRun,
        overdue_secs: int,
    ) -> None:
        """Count working_memory rows whose expiry has passed by > overdue_secs.

        CockroachDB's row TTL job fires periodically; a non-zero count here is
        expected if the job has not run since the rows expired. A large count
        suggests the TTL job is behind or disabled.
        """
        try:
            row = conn.execute(
                """
                SELECT count(*) AS n
                FROM working_memory
                WHERE tenant_id = %s
                  AND expiry < NOW() - INTERVAL '%s seconds'
                """,
                (str(tenant_id), overdue_secs),
            ).fetchone()
            overdue = int(row["n"]) if row else 0  # type: ignore[index]
        except Exception as exc:
            logger.warning("a8_ttl_check_failed", tenant_id=str(tenant_id), error=str(exc))
            return

        run.working_memory_overdue_count = overdue
        if overdue > 0:
            logger.warning(
                "a8_working_memory_ttl_behind",
                tenant_id=str(tenant_id),
                overdue_count=overdue,
                overdue_threshold_secs=overdue_secs,
                message=(
                    "CockroachDB TTL job may be behind. "
                    "Run: SHOW JOBS; or check TTL settings on working_memory table."
                ),
            )
        else:
            logger.info(
                "a8_working_memory_ttl_healthy",
                tenant_id=str(tenant_id),
                overdue_threshold_secs=overdue_secs,
            )
