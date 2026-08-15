# P9 Decision: A8 Consolidation Agent

**Status:** BUILT  
**Date:** 2025-01-15  
**Owner:** E3 (containment / integrity hygiene)

---

## Decision

Build A8 as a periodic hygiene agent that merges exact-duplicate trusted beliefs, flags long supersession chains for human review, and verifies that CockroachDB's row-TTL job is keeping working_memory clean.

## Context

Over time the belief store accumulates noise in three forms:

1. **Exact duplicates**: multiple agents ingest the same fact (same subject, predicate, object). Each gets a unique `belief_id` and passes screening independently. Without compaction, the semantic layer must arbitrate among identical trusted beliefs — wasteful and confusing.
2. **Long supersession chains**: repeated updates to a fact produce chains like B1 → B2 → B3 → … → BN. Chains degrade recall performance (the resolver must walk the chain) and indicate pathological update patterns.
3. **Working memory leakage**: CockroachDB row TTL is managed by a background job that runs asynchronously. If the job falls behind, expired context bleeds into active recall. A8 detects this by counting overdue rows, enabling an operator alert.

## What We Built

**Step 1 — Exact-duplicate compaction**:
- Query: `GROUP BY (subject, predicate, object_normalized) HAVING count(*) > 1` in `status='trusted'`.
- Winner selection: highest confidence; ties broken by most-recent `valid_from`.
- Losers: `status → 'superseded'`, `valid_to` set, `superseded_by → winner`. Not deleted — audit trail must be preserved (Security Invariant 6).
- T9 boundary check: before merging any group, all `belief_id`s are checked against the `quarantine` table. If any member was ever quarantined, the entire group is skipped. Merging quarantined content into clean lineage is a memory-corruption vector.

**Step 2 — Long chain flagging**:
- Heuristic: count in-degree on the `superseded_by` graph. Roots with high in-degree indicate long chains.
- Chains longer than `max_chain_depth` (default 10) are logged and appended to `run.chain_flags`.
- **No automatic collapsing** — human review is required for structural rewrites of the supersession graph.

**Step 3 — Working memory TTL verification**:
- Count `working_memory` rows where `expiry < NOW() - threshold` (default 1 hour).
- Non-zero counts trigger a warning log with instructions to check `SHOW JOBS` in CockroachDB.
- Failure to query this table is non-fatal: the run continues and logs a warning.

**dry_run mode**: A8 can be run with `dry_run=True` to detect duplicates and chains without applying any changes. Useful for auditing before enabling automatic compaction in production.

## Why Conservative by Default

An over-eager consolidator is itself a memory-corruption vector. If A8 merged groups incorrectly (e.g., collapsing across tenant boundaries, or collapsing beliefs with different provenance semantics), it would silently destroy audit-provenance. The design philosophy is:

- When uncertain, do nothing.
- Never delete — only supersede.
- Never cross the quarantine boundary.
- Never auto-collapse long chains — flag and defer to a human.

## Scheduling

A8 is designed to run periodically (e.g., hourly via a cron job or scheduler). It is not in the hot path. The agent is stateless — the connection is passed in, and the run produces a `ConsolidationRun` result object.

## Alternatives Considered

- **Real-time deduplication in A7 (resolve)**: would prevent duplicates from entering, but A7 runs per-belief under serializable isolation — adding a group-scan would significantly increase contention and latency.
- **Deleting losers instead of superseding**: would lose the audit trail. Rejected — see Security Invariant 6.
- **Full graph traversal for chain detection**: would be cycle-safe but expensive. The in-degree heuristic is sufficient to detect pathological chains without O(N²) queries.
