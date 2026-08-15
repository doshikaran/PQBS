# P9 Decision: A16 Federation / Trust Sharding

**Status:** SKIP (deferred)  
**Date:** 2025-01-15  
**Owner:** Lead

---

## Decision

Do not implement A16 (Federation / Trust Sharding) in Phase 9.

## What A16 Would Do

A16 would partition the screening workload by trust tier. High-trust sources (TrustTier.AUTHORITATIVE) would bypass or fast-path through A4 screening; low-trust sources (TrustTier.UNVERIFIED) would receive full signal evaluation. This reduces total screening latency and queue depth under load.

Specifically:
- A16 would intercept at ingestion time, route beliefs to a tier-specific screening worker.
- AUTHORITATIVE tier beliefs might skip S1 (embedding anomaly) and S2 (source trust tier, which would trivially score 0.0 for authoritative sources) and proceed directly to fast-track gate with only S5–S8.
- UNVERIFIED tier beliefs would go through the full 8-signal pipeline.

## Why Skipped

**Complexity vs. impact**: the T10 screening starvation problem is already mitigated by A13 (rate limiting). A16 would provide additional throughput but at the cost of introducing routing logic, per-tier worker pools, and a new blast surface where a miscategorized belief could bypass screening.

**Trust tier assignment is itself untrusted**: the `source_trust_tier` on `ProvenanceRecord` is set by the ingestion agent. Routing decisions based on it would give producers indirect control over which signals fire — a privilege escalation vector.

**The screening fast-path violates the spirit of Security Invariant 5**: the gate is fail-closed. A fast-path is a non-closed path. Even a partial bypass introduces residual risk that is hard to reason about under adversarial assumptions.

**Build order**: A16 should come after A13 is proven insufficient in production load testing. Building A16 before we have evidence that A13 is the bottleneck is premature optimization.

## Deferral Conditions

Implement A16 when:
1. Production profiling shows screening queue depth is the binding constraint even with A13 throttling in place.
2. A trusted, external attestation mechanism (e.g., mTLS certificate from a known service identity) can authenticate trust tier claims, reducing the privilege-escalation risk of fast-path routing.
3. The fast-path code path is subject to independent security review before deployment.
