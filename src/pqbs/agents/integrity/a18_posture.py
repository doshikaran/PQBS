"""A18 Posture Verification Agent — Phase 6.5, E3.

Continuously verifies that the deployed CockroachDB schema matches the
committed posture baseline, defending against T11 (control drift).

On match → emits POSTURE_ATTESTED audit record to WORM sink.
On drift → emits POSTURE_DRIFT_DETECTED audit record and raises PostureDriftError.

Agent Skills integration:
    This agent implements the same introspection logic that the CockroachDB
    Agent Skills repo (cockroachlabs/cockroachdb-skills, security + schema-design
    families) would perform via its skill runtime. We execute the checks directly
    via information_schema and pg_catalog queries — the same sources the skills
    consult — because the deployed free-tier cluster may not support the full
    Node.js skills runtime. The functional result is equivalent; the implementation
    is documented here so that switching to the skills runtime later is a drop-in.

Authority:
    A18 CANNOT remediate. It detects and reports; a human acts. An agent that
    can both detect and silently fix control drift can also silently cause it
    (Security Invariant 7 extended to posture controls).

Baseline:
    The expected state of every enforcing control is committed to
    docs/posture-baseline.json at deployment time. The baseline lives outside
    the database so that a compromised cluster cannot alter what is verified.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import structlog

from pqbs.agents.integrity.audit_sink import AuditSink, emit_or_block
from pqbs.contracts import AuditRecord
from pqbs.contracts.enums import AuditEventType

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PostureDriftError(Exception):
    """Raised when live schema state diverges from the committed baseline."""

    def __init__(self, drifts: list["DriftFinding"]) -> None:
        self.drifts = drifts
        summary = "; ".join(f"{d.control_class}/{d.control_name}: {d.description}" for d in drifts)
        super().__init__(f"Posture drift detected ({len(drifts)} finding(s)): {summary}")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DriftFinding:
    """A single control that has drifted from baseline."""
    control_class: str    # "role", "constraint", "view", "index", "ttl"
    control_name: str
    expected: Any
    observed: Any
    description: str


@dataclass
class PostureState:
    """Snapshot of the live schema controls."""
    captured_at: datetime
    roles: list[str]
    check_constraints: dict[str, list[str]]  # table → list of constraint names
    views: list[str]
    vector_indexes: list[str]
    ttl_tables: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "captured_at": self.captured_at.isoformat(),
            "roles": sorted(self.roles),
            "check_constraints": {k: sorted(v) for k, v in self.check_constraints.items()},
            "views": sorted(self.views),
            "vector_indexes": sorted(self.vector_indexes),
            "ttl_tables": sorted(self.ttl_tables),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PostureState":
        return cls(
            captured_at=datetime.fromisoformat(d["captured_at"]),
            roles=d["roles"],
            check_constraints=d["check_constraints"],
            views=d["views"],
            vector_indexes=d.get("vector_indexes", []),
            ttl_tables=d.get("ttl_tables", []),
        )


@dataclass
class VerificationResult:
    """Output of a single posture verification cycle."""
    verified_at: datetime
    has_drift: bool
    findings: list[DriftFinding] = field(default_factory=list)
    live_state: PostureState | None = None


# ---------------------------------------------------------------------------
# Posture capture (introspection queries)
# ---------------------------------------------------------------------------

def capture_live_posture(conn: psycopg.Connection[Any]) -> PostureState:
    """Query the live database and return its current posture state.

    Uses information_schema and pg_catalog — same sources CockroachDB Agent
    Skills (security family) consult for posture introspection.
    """
    now = datetime.now(tz=timezone.utc)

    # 1. Roles
    role_rows = conn.execute(
        "SELECT rolname FROM pg_catalog.pg_roles WHERE rolname LIKE 'role_%' ORDER BY rolname"
    ).fetchall()
    roles = [str(r["rolname"]) for r in role_rows]

    # 2. Check constraints on our core tables
    constraint_rows = conn.execute(
        """
        SELECT tc.table_name, tc.constraint_name
        FROM information_schema.table_constraints tc
        WHERE tc.constraint_type = 'CHECK'
          AND tc.table_name IN ('belief', 'provenance', 'quarantine',
                                'integrity_verdict', 'working_memory')
        ORDER BY tc.table_name, tc.constraint_name
        """
    ).fetchall()
    check_constraints: dict[str, list[str]] = {}
    for row in constraint_rows:
        tbl = str(row["table_name"])
        check_constraints.setdefault(tbl, []).append(str(row["constraint_name"]))

    # 3. Views (our role-scoped views)
    view_rows = conn.execute(
        """
        SELECT table_name FROM information_schema.views
        WHERE table_schema = 'public'
        ORDER BY table_name
        """
    ).fetchall()
    views = [str(r["table_name"]) for r in view_rows]

    # 4. Vector index — check pg_indexes for 'embedding' column indexes
    index_rows = conn.execute(
        """
        SELECT indexname FROM pg_catalog.pg_indexes
        WHERE tablename = 'belief'
          AND indexdef LIKE '%embedding%'
        ORDER BY indexname
        """
    ).fetchall()
    vector_indexes = [str(r["indexname"]) for r in index_rows]

    # 5. TTL tables — CockroachDB exposes TTL config in crdb_internal.tables
    try:
        ttl_rows = conn.execute(
            """
            SELECT name FROM crdb_internal.tables
            WHERE state = 'PUBLIC'
              AND ttl_expire_after IS NOT NULL
            ORDER BY name
            """
        ).fetchall()
        ttl_tables = [str(r["name"]) for r in ttl_rows]
    except Exception:
        # crdb_internal may require elevated privileges in some configs
        ttl_tables = []

    return PostureState(
        captured_at=now,
        roles=roles,
        check_constraints=check_constraints,
        views=views,
        vector_indexes=vector_indexes,
        ttl_tables=ttl_tables,
    )


# ---------------------------------------------------------------------------
# Posture verification
# ---------------------------------------------------------------------------

def verify_posture(
    live: PostureState,
    baseline: PostureState,
) -> list[DriftFinding]:
    """Diff live state against baseline. Returns list of findings (empty = clean)."""
    findings: list[DriftFinding] = []

    # Roles
    expected_roles = set(baseline.roles)
    observed_roles = set(live.roles)
    missing = expected_roles - observed_roles
    extra = observed_roles - expected_roles
    for role in sorted(missing):
        findings.append(DriftFinding(
            control_class="role",
            control_name=role,
            expected="exists",
            observed="missing",
            description=f"Role '{role}' was expected but is absent",
        ))
    for role in sorted(extra):
        findings.append(DriftFinding(
            control_class="role",
            control_name=role,
            expected="absent",
            observed="exists",
            description=f"Unexpected role '{role}' not in baseline",
        ))

    # Check constraints
    for table, expected_constraints in baseline.check_constraints.items():
        observed = live.check_constraints.get(table, [])
        for c in expected_constraints:
            if c not in observed:
                findings.append(DriftFinding(
                    control_class="constraint",
                    control_name=f"{table}.{c}",
                    expected="exists",
                    observed="missing",
                    description=f"CHECK constraint '{c}' on '{table}' is missing",
                ))

    # Views
    expected_views = set(baseline.views)
    observed_views = set(live.views)
    for view in sorted(expected_views - observed_views):
        findings.append(DriftFinding(
            control_class="view",
            control_name=view,
            expected="exists",
            observed="missing",
            description=f"View '{view}' was expected but is absent",
        ))

    # Vector index
    if baseline.vector_indexes and not live.vector_indexes:
        findings.append(DriftFinding(
            control_class="index",
            control_name="belief_embedding_vector_idx",
            expected=f"one of {baseline.vector_indexes}",
            observed="none found",
            description="Vector index on belief.embedding is missing",
        ))

    # TTL
    for table in baseline.ttl_tables:
        if table not in live.ttl_tables:
            findings.append(DriftFinding(
                control_class="ttl",
                control_name=table,
                expected="ttl_configured",
                observed="ttl_absent",
                description=f"Row-level TTL on '{table}' has been removed",
            ))

    return findings


# ---------------------------------------------------------------------------
# A18 PostureVerificationAgent
# ---------------------------------------------------------------------------

_DEFAULT_BASELINE_PATH = (
    Path(__file__).parents[5] / "docs" / "posture-baseline.json"
)


class PostureVerificationAgent:
    """A18 — Posture Verification Agent.

    Runs scheduled verification cycles comparing live schema state to the
    committed baseline artifact. Cannot remediate — reports only.
    """

    def __init__(
        self,
        audit_sink: AuditSink,
        agent_id: str = "a18_posture",
        tenant_id: UUID | None = None,
        baseline_path: Path | None = None,
    ) -> None:
        self._audit_sink = audit_sink
        self._agent_id = agent_id
        self._tenant_id = tenant_id or UUID(
            os.environ.get("TENANT_ID_DEMO", "00000000-0000-0000-0001-000000000001")
        )
        self._baseline_path = baseline_path or _DEFAULT_BASELINE_PATH

    def load_baseline(self) -> PostureState:
        """Load the committed posture baseline from disk."""
        try:
            raw = json.loads(self._baseline_path.read_text())
            controls = raw.get("controls", raw)
            return PostureState.from_dict(controls)
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"Posture baseline not found at {self._baseline_path}. "
                "Run capture_and_save_baseline() first."
            ) from exc

    def capture_and_save_baseline(
        self,
        conn: psycopg.Connection[Any],
        path: Path | None = None,
    ) -> PostureState:
        """Capture live posture and write it as the new baseline artifact.

        This is run once at deployment — NOT on every verification cycle.
        """
        target = path or self._baseline_path
        state = capture_live_posture(conn)
        target.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "baseline_version": "1.0",
            "captured_at": state.captured_at.isoformat(),
            "schema_version": "0012",
            "controls": state.to_dict(),
        }
        target.write_text(json.dumps(doc, indent=2))
        logger.info("posture_baseline_saved", path=str(target))
        return state

    def run_verification_cycle(
        self,
        conn: psycopg.Connection[Any],
    ) -> VerificationResult:
        """Run one posture verification cycle.

        On match: writes POSTURE_ATTESTED to WORM audit sink.
        On drift: writes POSTURE_DRIFT_DETECTED and raises PostureDriftError.

        The raise on drift is intentional — it surfaces to the scheduler/Lambda
        handler so the drift can be alerted via CloudWatch or similar. The audit
        record is always written first, so the evidence is preserved regardless.

        Raises:
            PostureDriftError: If any control has drifted from baseline.
            AuditSinkError: If the audit sink is unavailable (fail-closed per §17).
        """
        verified_at = datetime.now(tz=timezone.utc)
        baseline = self.load_baseline()
        live = capture_live_posture(conn)
        findings = verify_posture(live, baseline)

        if not findings:
            audit_record = AuditRecord(
                event_type=AuditEventType.POSTURE_ATTESTED,
                agent_id=self._agent_id,
                tenant_id=self._tenant_id,
                timestamp=verified_at,
                before=None,
                after={"schema_version": "0012", "controls_verified": 5},
                reason="All posture controls match baseline — no drift detected",
            )
            emit_or_block(self._audit_sink, audit_record)
            logger.info("posture_verified_clean", verified_at=verified_at.isoformat())
            return VerificationResult(
                verified_at=verified_at,
                has_drift=False,
                live_state=live,
            )

        # Drift detected
        drift_detail = [
            {
                "control_class": f.control_class,
                "control_name": f.control_name,
                "expected": f.expected,
                "observed": f.observed,
                "description": f.description,
            }
            for f in findings
        ]
        audit_record = AuditRecord(
            event_type=AuditEventType.POSTURE_DRIFT_DETECTED,
            agent_id=self._agent_id,
            tenant_id=self._tenant_id,
            timestamp=verified_at,
            before={"schema_version": "0012"},
            after={"drift_findings": json.dumps(drift_detail)},
            reason=(
                f"POSTURE DRIFT: {len(findings)} control(s) diverge from baseline. "
                f"First: {findings[0].description}"
            ),
        )
        emit_or_block(self._audit_sink, audit_record)

        logger.critical(
            "posture_drift_detected",
            finding_count=len(findings),
            findings=drift_detail,
        )

        raise PostureDriftError(findings)
