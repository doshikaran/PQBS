# P9 Decision: A13 Rate Limiting & Admission Agent

**Status:** BUILT  
**Date:** 2025-01-15  
**Owner:** E1 (producer path)

---

## Decision

Build A13 as a synchronous admission gate called at the top of `ingest()`, before any Bedrock call, enforcing per-agent hourly write quotas and tenant-level screening queue depth caps.

## Context

Phase 9 extensions include optional multi-tenancy hardening. The primary risk is **T10 — Screening Starvation**: a compromised or misbehaving producer floods the gate with beliefs faster than A4 can screen them, starving legitimate beliefs of trust verdicts and degrading recall quality.

T10 is also a denial-of-service vector against the screening tier: sustained flooding increases gate latency, which increases memory pressure in the lambda screener, which can cause OOM crashes.

## What We Built

- **Per-agent hourly quota** (default 1000 writes/hour, env-configurable): counted against committed writes in `belief.tx_from`. Quota resets on a rolling 1-hour window, not a fixed clock boundary.
- **Tenant-level queue depth cap** (default 5000 pending beliefs, env-configurable): if the screening backlog exceeds this, new writes are throttled until the gate clears.
- **Fail-safe semantics**: DB errors during quota check → throttle (not reject, not admit). A delayed write is recoverable; a lost write is not. This is the conservative choice.
- **Reject vs. throttle distinction**: quota exhaustion is a hard reject (the agent cannot retry immediately); queue overflow is a throttle (the agent should retry after a delay). The error types (`AdmissionRejectedError`, `AdmissionThrottledError`) encode this distinction so callers can surface it correctly to producers.
- **No in-memory counters**: counting is always against the live `belief` table. Counters that reset on process recycle can be bypassed by restarting the agent.

## Integration Point

`AdmissionAgent().enforce(author_agent_id, tenant_id, conn)` is called at the top of `ingest()` in `a1_ingest.py`, before the Bedrock extraction call. This ensures we pay no API cost for rejected writes.

## Alternatives Considered

- **A16 Federation / trust sharding**: would partition screening workload across trust tiers, reducing per-tier queue depth. Deferred — see `P9-a16-federation.md`.
- **Token bucket in Redis**: more precise rate limiting, but introduces an external stateful dependency. The SQL-count approach is sufficient at hackathon scale and avoids the operational complexity.
- **Reject on quota**: considered always rejecting on DB failure, but fail-closed on admission would block all writes when the DB is degraded — exactly when we need the system to remain writable.

## Security Properties

- T10 (screening starvation) mitigated: quota prevents unbounded write floods.
- No bypass via process restart: quota is counted from the DB, not an in-memory counter.
- Security Invariant 7 preserved: A13 checks quotas but writes nothing — it has no write authority.
