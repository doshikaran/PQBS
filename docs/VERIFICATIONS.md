## V5 — Serializable Retry Determinism

**Run date:** 2026-08-13 08:19 UTC
**Cluster:** CockroachDB Serverless (COCKROACH_URL)
**Runs:** 25
**Conflicts detected:** 0/25 (0.0%)
**Pass criterion:** ≥90% of runs
**Result:** ❌ FAILED

**SQLSTATE observed:** `none`

**Timing window:** Both threads read row id=1 under SERIALIZABLE isolation,
synchronised at a barrier, then each attempted to write a distinct value.
Jitter of 1–8ms applied between barrier and write.
Average pair duration: 1744ms.

**Findings:**
- Serializable isolation is NOT reliably producing SQLSTATE 40001
  on concurrent read-write conflicts.
- The retry wrapper (Phase 3, E1) must catch `psycopg.errors.SerializationFailure`
  (pgcode `40001`).
- The Phase 3 / Phase 8 contention harness (`tests/contention/`) will use the same
  timing pattern with 8–16 concurrent writers.

**Active fallback:** V5 FAILED. Demo narrative must shift to quarantine + temporal reconstruction as the central moments. Document this decision before proceeding to Phase 1. See BUILD-PLAN.md §2.7.