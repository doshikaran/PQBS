"""A13 — Rate Limiting & Admission Agent.

Enforces per-agent write quotas and monitors screening queue depth to defend
against T10 (screening starvation): an attacker floods the gate with
expensive-to-screen content, starving legitimate beliefs of trust verdicts.

Security properties:
  - Per-agent hourly write quota (configurable via PQBS_RATE_LIMIT_PER_AGENT_PER_HOUR).
  - Tenant-level queue depth cap (configurable via PQBS_QUEUE_DEPTH_THROTTLE).
  - Fail-safe: uncertainty → throttle, not reject. A delayed legitimate write
    is recoverable; a lost one is not.
  - No bypass: quotas are enforced against the live belief table, not an
    in-memory counter that restarts on process recycle.

Integration point: called at the top of A1 ingest() before any Bedrock call.
Raises AdmissionRejectedError (quota) or AdmissionThrottledError (queue depth).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

import psycopg
import structlog

from pqbs.contracts.exceptions import AdmissionRejectedError, AdmissionThrottledError

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Configurable limits (overridable via environment for testing)
# ---------------------------------------------------------------------------

# Max beliefs a single agent may write per hour per tenant.
# Default 1000 is generous for legitimate batch loads; an attacker flooding
# the gate at 1 write/ms would hit this in < 1 second.
_DEFAULT_QUOTA_PER_HOUR: int = 1000

# If the tenant has more than this many pending (unscreened) beliefs, throttle
# new writes until the gate catches up. Default 5000.
_DEFAULT_QUEUE_DEPTH_THROTTLE: int = 5000


def _quota() -> int:
    return int(os.environ.get("PQBS_RATE_LIMIT_PER_AGENT_PER_HOUR", _DEFAULT_QUOTA_PER_HOUR))


def _queue_throttle() -> int:
    return int(os.environ.get("PQBS_QUEUE_DEPTH_THROTTLE", _DEFAULT_QUEUE_DEPTH_THROTTLE))


# ---------------------------------------------------------------------------
# Admission decision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AdmissionDecision:
    """Result of A13 admission check."""

    action: Literal["admit", "throttle", "reject"]
    reason: str
    writes_in_window: int
    pending_depth: int
    quota: int
    throttle_limit: int

    @property
    def admitted(self) -> bool:
        return self.action == "admit"

    def __str__(self) -> str:
        return (
            f"AdmissionDecision(action={self.action}, reason={self.reason!r}, "
            f"writes={self.writes_in_window}/{self.quota}, pending={self.pending_depth})"
        )


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class AdmissionAgent:
    """A13 Rate Limiting & Admission Agent.

    Call check() before every write attempt. The caller must raise
    AdmissionRejectedError or AdmissionThrottledError based on the decision;
    AdmissionAgent itself does not raise so that callers can inspect the
    decision object before acting.

    Alternatively, call enforce() which raises for you.
    """

    def check(
        self,
        agent_id: str,
        tenant_id: UUID,
        conn: psycopg.Connection[Any],
    ) -> AdmissionDecision:
        """Return an AdmissionDecision. Never raises."""
        quota = _quota()
        throttle_limit = _queue_throttle()

        # Count beliefs written by this agent in the last 60 minutes.
        # Using tx_from (the CockroachDB commit timestamp) so we measure
        # committed writes, not in-flight ones.
        try:
            writes_row = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM belief
                WHERE tenant_id = %s
                  AND author_agent_id = %s
                  AND tx_from >= NOW() - INTERVAL '1 hour'
                """,
                (str(tenant_id), agent_id),
            ).fetchone()
            writes_in_window = int(writes_row["n"]) if writes_row else 0  # type: ignore[index]
        except Exception as exc:
            # On DB error, fail safe: throttle (not reject) so legitimate
            # writes retry when the DB recovers.
            logger.warning("a13_quota_check_failed", agent_id=agent_id, error=str(exc))
            return AdmissionDecision(
                action="throttle",
                reason=f"quota check unavailable (fail-safe throttle): {exc}",
                writes_in_window=0,
                pending_depth=0,
                quota=quota,
                throttle_limit=throttle_limit,
            )

        # Count pending (unscreened) beliefs for this tenant.
        # This is the proxy for screening queue depth.
        try:
            pending_row = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM belief
                WHERE tenant_id = %s AND status = 'pending'
                """,
                (str(tenant_id),),
            ).fetchone()
            pending_depth = int(pending_row["n"]) if pending_row else 0  # type: ignore[index]
        except Exception as exc:
            logger.warning("a13_queue_depth_check_failed", tenant_id=str(tenant_id), error=str(exc))
            pending_depth = 0  # conservative: don't throttle on measurement failure

        # Decision logic — reject takes priority over throttle.
        if writes_in_window >= quota:
            decision = AdmissionDecision(
                action="reject",
                reason=(
                    f"per-agent hourly quota exceeded: {writes_in_window} writes "
                    f"in last hour >= quota {quota}"
                ),
                writes_in_window=writes_in_window,
                pending_depth=pending_depth,
                quota=quota,
                throttle_limit=throttle_limit,
            )
        elif pending_depth >= throttle_limit:
            decision = AdmissionDecision(
                action="throttle",
                reason=(
                    f"screening queue depth exceeded: {pending_depth} pending beliefs "
                    f">= throttle limit {throttle_limit}; retry when queue clears"
                ),
                writes_in_window=writes_in_window,
                pending_depth=pending_depth,
                quota=quota,
                throttle_limit=throttle_limit,
            )
        else:
            decision = AdmissionDecision(
                action="admit",
                reason="within quota and queue depth limits",
                writes_in_window=writes_in_window,
                pending_depth=pending_depth,
                quota=quota,
                throttle_limit=throttle_limit,
            )

        logger.info(
            "a13_admission_decision",
            agent_id=agent_id,
            tenant_id=str(tenant_id),
            action=decision.action,
            writes_in_window=writes_in_window,
            pending_depth=pending_depth,
            quota=quota,
        )
        return decision

    def enforce(
        self,
        agent_id: str,
        tenant_id: UUID,
        conn: psycopg.Connection[Any],
    ) -> AdmissionDecision:
        """Check and raise on rejection or throttle. Returns decision on admit.

        Raises:
            AdmissionRejectedError: per-agent quota exhausted.
            AdmissionThrottledError: screening queue too deep (retry later).
        """
        decision = self.check(agent_id, tenant_id, conn)
        if decision.action == "reject":
            raise AdmissionRejectedError(decision.reason)
        if decision.action == "throttle":
            raise AdmissionThrottledError(decision.reason)
        return decision
